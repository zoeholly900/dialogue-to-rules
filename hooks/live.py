#!/usr/bin/env python3
"""D2R_MODE=live (default): write the rule in the turn the user corrects you. The Stop / 撤 half works in
every mode; only live and sweep teach the convention here (nudge / regex do it per message in detect.py).

One entry point for three Claude Code hooks, dispatched on hook_event_name:
  SessionStart      -> tell the model the one-line convention (once per window, again after a compact),
                       turn anything still in the old y/n queue into rules, and hand over the notices
                       of rules that went live while the user was away (sweep, migrate).
  Stop              -> read the reply that just ended; every line "📌 记下了：当X就Y。不要就说「撤」"
                       is written as a rule right away, by this script, with the user's last message as
                       evidence. No tool call, no permission prompt, no question.
  UserPromptSubmit  -> remember the user's message (it is the evidence for the Stop above); if it is
                       just "撤" / "不要这条" and the reply before it announced a rule, deprecate that rule
                       and tell the model to say so in one line.
The user only ever needs one word: 撤. Undo, deprecate <id>, list stay in scripts/d2r.py for whoever wants them.

State: <project>/.d2r/live_state.json (per session: last prompt, rules the last reply announced).
Failures never interrupt: one line to <project>/.d2r/live.log; a rule that was announced but could not be
written is reported to the user with a systemMessage, so "记下了" is never a lie.
Every rule written or taken back also shows a system notification (macOS osascript, Linux notify-send,
otherwise nothing): it never blocks, needs no click, and stays in the notification centre, so the user
knows what was added even when the 📌 line scrolled past. D2R_NOTIFY=0 turns it off.
Clicking a banner opens the rule book (scripts/d2r.py render_book) at that rule; that needs terminal-notifier,
which is fetched once into <plugin>/vendor/ from its GitHub release when missing (until then: osascript,
not clickable, ends with "看全部：说「规矩本」"). The user saying 规矩本 gets the page's path.
Env: D2R_MODE, D2R_OFF=1, D2R_NOTIFY=0, D2R_PROJECT, D2R_HOME, D2R_HOST. Standard library only.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import d2r  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "d2r.py")
TASK_RULES_SHOWN = 20
MAX_PER_REPLY = 2          # a reply that "records" more than this is not following the convention
QUOTE_WHOLE = 120          # a user message up to this long is the evidence as it is
STATE_DAYS = 7             # sessions untouched this long are dropped from live_state.json
NOTIFY_CHARS = 60          # a banner shows about two lines; the full rule is in CLAUDE.md
BOOK_HINT = "\n看全部：说「规矩本」"   # where a banner cannot be clicked open
VENDOR = os.path.join(ROOT, "vendor")
TN_APP = os.path.join(VENDOR, "terminal-notifier.app")
TN_BIN = os.path.join(TN_APP, "Contents", "MacOS", "terminal-notifier")
TN_STATE = os.path.join(VENDOR, "install.json")
TN_URL = "https://github.com/julienXX/terminal-notifier/releases/download/2.0.0/terminal-notifier-2.0.0.zip"
TN_SHA256 = None           # not pinned yet (github.com was unreachable when this was written): the HTTPS
                           # download from the official release is trusted and its digest kept in install.json
TN_RETRY = 86400           # a failed download is tried again a day later, not on every banner

MARK_RX = re.compile(r"^[ \t>*-]*📌\s*(?:\*\*)?记下了(?P<all>[（(]所有项目[）)])?(?:\*\*)?[：:]\s*(?P<body>.+?)\s*$", re.M)
FENCE_RX = re.compile(r"```.*?(?:```|\Z)", re.S)
TAIL_RX = re.compile(r"[。.；;，,\s]*(?:不要|不想要|不对)的?话?就?(?:说|回)\s*[「『“‘\"']?撤[」』”’\"']?.*$")
BOOK_RX = re.compile(r"^\s*(?:看看?|打开|给我看看?|看下)?\s*(?:我的)?\s*规矩本\s*(?:呢|在哪|在哪儿|在哪里)?\s*[。.？?！!]*\s*$")
TEMPLATE_RX = re.compile(r"<[^>]*(?:情形|怎么做|触发)[^>]*>|……")
RETRACT_RX = re.compile(
    r"^\s*(?:撤|撤掉|撤了|撤回|撤吧|撤了吧|撤掉吧|撤销|这条撤了|这条撤掉|撤这条|撤掉这条|"
    r"不要这条|不要这条了|这条不要|这条不要了|这条别记|别记这条|别记|别记了|不用记|不用记了|不要记)"
    r"\s*[。.！!～~]*\s*$")

LIVE_RULES = """dialogue-to-rules 开着：用户纠正你时，你当场记下，不问他要不要记。
- 什么时候记：两条都要满足。①用户自己说明了这是一条规矩：话里有「以后」「每次」「一律」「所有」「别再」「又」「说过」「早说了」这类意思，或者在指出你重复犯的错；②这件事换个任务、换个文件还会再碰到（通用的、反复碰到的工作习惯）。
- 用户只是对这一次的成品提修改意见（改语气、换个词、调长短、改标题、改配色），哪怕你觉得能推广，也不记，照改就行。
- 怎么记：照常回复、照常把活干完，在这条回复的最后单独一行写：
  📌 记下了：当<什么情形>就<怎么做>。不要就说「撤」
  这一行一写出来插件就自动存成规则，你不用跑任何命令。「当…就…」写成换个任务也能照做的具体动作，不带这一单的人名文件名，只写这一次纠的那件事，不扩大。
- 这件事明显不分项目都成立（用户说了「以后都」「所有窗」「通用」，或者本来就是不分项目的做事习惯），写成「📌 记下了（所有项目）：…」；拿不准就不加，落在本项目。
- 也不记：只在这一单成立的细节、闲聊里的澄清、玩笑、纯情绪。可记可不记的不记，宁可少记不要乱记。一条回复最多一行。没记就什么都别提，也别解释这条规矩。
- 用户回「撤」「不要这条」：插件会自动作废刚记的那条，你回一句撤掉了哪条就行。要撤更早的，跑 python3 "{script}" list 找到号，再 python3 "{script}" deprecate <号>。"""


# ---------- small helpers ----------

def mode():
    return os.environ.get("D2R_MODE") or "live"


def active():
    """Writing the 📌 line and 撤 work in every mode (nudge / regex ask for the same line)."""
    return os.environ.get("D2R_OFF") != "1" and os.environ.get("D2R_SWEEP_CHILD") != "1"


def teaches():
    """live / sweep tell the convention once per window; nudge / regex tell it per message (detect.py)."""
    return mode() in ("live", "sweep")


def project_of(payload):
    return os.path.abspath(os.environ.get("D2R_PROJECT") or os.environ.get("CLAUDE_PROJECT_DIR")
                           or payload.get("cwd") or os.getcwd())


def log(msg):
    try:
        path = os.path.join(d2r.project_dir(), ".d2r", "live.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{d2r.now()} {d2r.one_line(msg)[:400]}\n")
    except OSError:
        pass


def host():
    return os.environ.get("D2R_HOST") or "claude"


# ---------- state ----------

def state_path():
    return os.path.join(d2r.project_dir(), ".d2r", "live_state.json")


class Locked:
    """Read-modify-write live_state.json under a lock (several windows can share one project)."""

    def __enter__(self):
        self.f = None
        try:
            import fcntl
            path = os.path.join(d2r.project_dir(), ".d2r", "live.lock")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.f = open(path, "w")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            self.state = json.loads(d2r.read_text(state_path()) or "{}")
        except ValueError:
            self.state = {}
        self.state.setdefault("sessions", {})
        return self

    def session(self, sid):
        s = self.state["sessions"].setdefault(sid or "unknown", {})
        s["t"] = time.time()
        return s

    def __exit__(self, exc_type, *exc):
        if exc_type is None:
            cut = time.time() - STATE_DAYS * 86400
            self.state["sessions"] = {k: v for k, v in self.state["sessions"].items() if v.get("t", 0) >= cut}
            d2r.atomic_write(state_path(), json.dumps(self.state, ensure_ascii=False))
        if self.f:
            self.f.close()


# ---------- reading the reply ----------

def marks(text):
    """-> [(body, all_projects)] for each 📌 记下了 line outside code blocks."""
    out = []
    for m in MARK_RX.finditer(FENCE_RX.sub("", text or "")):
        body = TAIL_RX.sub("", m.group("body")).strip().strip("*").strip()
        body = re.sub(r"[。.；;\s]+$", "", body)
        if not body or TEMPLATE_RX.search(body):  # an unfilled template, e.g. when the plugin is being discussed
            continue
        out.append((body, bool(m.group("all"))))
    return out


def to_card(body):
    """当X就Y -> trigger X, action Y. Prefer the "，就" split; no 当/就 shape -> the whole line is the action."""
    s = re.sub(r"^当", "", body).strip()
    m = re.match(r"(.+?)[，,]\s*就(.+)", s) or re.match(r"(.+?)就(.+)", s)
    if body.startswith("当") and m:
        trigger, action = m.group(1).strip(), m.group(2).strip()
    else:
        trigger, action = "类似情形", body
    if re.match(r"^(?:不要|别|不许|不准|不再)", action):
        return {"trigger": trigger, "action": "", "prohibit": re.sub(r"^(?:不要|别|不许|不准|不再)", "", action)}
    return {"trigger": trigger, "action": action, "prohibit": ""}


def best_quote(message, card):
    """The user's own words, verbatim. A long message is cut to the sentence closest to the rule."""
    message = (message or "").strip()
    if len(message) <= QUOTE_WHOLE:
        return message
    target = card["trigger"] + card["action"] + card["prohibit"]
    grams = {target[i:i + 2] for i in range(len(target) - 1)}
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?；;\n])", message) if p.strip()]
    best = max(parts, key=lambda p: (sum(p.count(g) for g in grams), -abs(len(p) - 40)))
    return best[:QUOTE_WHOLE * 2]


def last_user_text(transcript):
    """Fallback evidence when UserPromptSubmit did not record the prompt."""
    last = ""
    try:
        with open(transcript, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("isSidechain") or rec.get("isMeta"):
                    continue
                msg = rec.get("message") or {}
                if (rec.get("type") or msg.get("role")) != "user":
                    continue
                c = msg.get("content")
                text = c.strip() if isinstance(c, str) else "\n".join(
                    b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text").strip()
                if text and not text.startswith(("<", "[Request interrupted", "Caveat:")):
                    last = text
    except (OSError, TypeError):
        pass
    return last


def last_assistant_text(transcript):
    """Fallback when the Stop payload has no last_assistant_message: all assistant text after the last user turn."""
    parts = []
    try:
        with open(transcript, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("isSidechain"):
                    continue
                msg = rec.get("message") or {}
                role = rec.get("type") or msg.get("role")
                c = msg.get("content")
                if role == "user" and (isinstance(c, str) or any(
                        isinstance(b, dict) and b.get("type") == "text" for b in (c or []))):
                    parts = []
                elif role == "assistant" and isinstance(c, list):
                    parts += [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
    except (OSError, TypeError):
        pass
    return "\n".join(parts)


def notifier():
    """terminal-notifier (its banners can open a page when clicked), from PATH or our own copy."""
    found = shutil.which("terminal-notifier")
    return found or (TN_BIN if os.access(TN_BIN, os.X_OK) else None)


def read_install_state():
    try:
        with open(TN_STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def maybe_install_notifier():
    """No terminal-notifier on a Mac: fetch one in the background, at most once a day. This banner falls back."""
    st = read_install_state()
    if st and time.time() - st.get("at", 0) < TN_RETRY:
        return
    try:
        d2r.atomic_write(TN_STATE, json.dumps({"at": time.time(), "ok": False, "err": "正在装"}, ensure_ascii=False))
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "install-notifier"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        log(f"terminal-notifier 没装成：{e}")


def install_notifier():
    """Download the official release into <plugin>/vendor/, check its hash, unpack, prove it runs.
    Nothing outside the plugin's folder is touched (no brew, no /usr/local)."""
    import hashlib
    import urllib.request
    tmp = os.path.join(VENDOR, ".download")
    err, digest = "", None
    try:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        os.makedirs(tmp)
        zpath = os.path.join(tmp, "tn.zip")
        with urllib.request.urlopen(TN_URL, timeout=120) as resp, open(zpath, "wb") as f:
            shutil.copyfileobj(resp, f)
        with open(zpath, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if TN_SHA256 and digest != TN_SHA256:
            raise ValueError(f"校验不对：{digest}")
        subprocess.run(["ditto", "-x", "-k", zpath, tmp], check=True, timeout=60, capture_output=True)
        app = next(os.path.join(dp, d) for dp, ds, _ in os.walk(tmp) for d in ds if d == "terminal-notifier.app")
        if os.path.isdir(TN_APP):
            shutil.rmtree(TN_APP)
        shutil.move(app, TN_APP)
        subprocess.run([TN_BIN, "-help"], timeout=20, capture_output=True)
        if not os.access(TN_BIN, os.X_OK):
            raise OSError("解开了但跑不起来")
    except Exception as e:  # noqa: BLE001 - any failure means "fall back to osascript"
        err = f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    d2r.atomic_write(TN_STATE, json.dumps({"at": time.time(), "ok": not err, "err": err, "url": TN_URL,
                                           "sha256": digest}, ensure_ascii=False))
    log("terminal-notifier 装好了" if not err else f"terminal-notifier 没装成：{err}")


def notify(title, body, subtitle="", rule=None):
    """Fire and forget a banner. Never waits, never raises, never a dialog.
    With a rule, clicking the banner opens the rule book at that rule (terminal-notifier only)."""
    if os.environ.get("D2R_NOTIFY") == "0":
        return
    body = body if len(body) <= NOTIFY_CHARS else body[:NOTIFY_CHARS - 1] + "…"
    try:
        target = d2r.jump_to(rule) if rule else d2r.book_path()
    except (OSError, ValueError, KeyError) as e:
        log(f"规矩本跳转页没写成：{e}")
        target = None
    sink = os.environ.get("D2R_NOTIFY_LOG")  # tests: record instead of popping
    try:
        if sink:
            with open(sink, "a", encoding="utf-8") as f:
                f.write(json.dumps({"title": title, "body": body, "subtitle": subtitle, "open": target},
                                   ensure_ascii=False) + "\n")
            return
        tn = notifier() if sys.platform == "darwin" else None
        if tn and target:
            cmd = [tn, "-title", title, "-subtitle", subtitle, "-message", body, "-group", "d2r",
                   "-open", pathlib.Path(target).as_uri()]
        elif sys.platform == "darwin" and shutil.which("osascript"):
            maybe_install_notifier()
            # text goes in as argv, so quotes in a rule cannot break the script
            cmd = ["osascript", "-e", "on run argv",
                   "-e", "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
                   "-e", "end run", body + BOOK_HINT, title, subtitle]
        elif shutil.which("notify-send"):
            cmd = ["notify-send", "-a", "dialogue-to-rules", title,
                   body + ("\n" + subtitle if subtitle else "") + BOOK_HINT]
        else:
            return
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        log(f"通知没发出：{e}")


# ---------- events ----------

def on_prompt(payload):
    prompt = payload.get("prompt") or ""
    with Locked() as st:
        s = st.session(payload.get("session_id"))
        announced = s.pop("announced", [])
        if BOOK_RX.match(prompt):
            s["announced"] = announced  # asking for the book does not use up the 撤
            path = d2r.render_book()
            return ("用户要看规矩本（插件替他记下的所有规矩，网页）。直接把路径给他，文件一行、所在文件夹一行，"
                    f"都从根目录写全、单独成行、后面不跟标点：\n{path}\n{os.path.dirname(path)}\n"
                    "不用列规则内容，不用打开文件。")
        if RETRACT_RX.match(prompt) and announced:
            r = d2r.retract(host(), among=announced)
            if r is not None:
                log(f"撤 {r['id']}")
                notify("已撤", "已撤：" + d2r.brief(r), rule=r)
                return ("用户刚说撤：插件已经把刚记的这条作废了（不删）——"
                        f"[{r['id']}] {d2r.brief(r)}。回一句告诉用户撤掉了这条就行，别再记它。"
                        f"要是用户其实不是在撤规则，跑 python3 \"{SCRIPT}\" undo 恢复。")
        s["prompt"] = prompt
    return None


def on_stop(payload):
    text = payload.get("last_assistant_message")
    if text is None:
        text = last_assistant_text(payload.get("transcript_path") or "")
    found = marks(text)[:MAX_PER_REPLY]
    with Locked() as st:
        s = st.session(payload.get("session_id"))
        shown = s.pop("notices_shown", [])
        announced = list(shown) if shown and "📌" in (text or "") else []
        if not found:
            s["announced"] = announced
            return None
        evidence = s.get("prompt") or last_user_text(payload.get("transcript_path") or "")
        written, failed = [], []
        try:
            existing = {d2r.brief(r): r["id"] for g in (False, True) for r in d2r.load(g) if r["status"] == "active"}
        except (OSError, ValueError, KeyError):
            existing = {}
        for body, all_projects in found:
            card = to_card(body)
            if d2r.brief(card) in existing:  # announced again (or the Stop fired twice): write once, 撤 still works
                written.append(existing[d2r.brief(card)])
                continue
            card.update(scope="global" if all_projects else "project",
                        evidence={"quote": best_quote(evidence, card) or "（没取到原话）"})
            try:
                r = d2r.add_rule(card, host(), source="live", session=payload.get("session_id"))
                written.append(r["id"])
                existing[d2r.brief(r)] = r["id"]
                log(f"记下 {r['id']}（{r['scope']}）：{d2r.brief(r)}")
                notify("记下一条规矩" + ("（所有项目）" if r["scope"] == "global" else ""), body, "不要就回「撤」", rule=r)
            except (SystemExit, Exception) as e:  # announced but not stored: the user must hear it
                failed.append(f"{body}（{e}）")
                log(f"没记上：{body}：{e}")
        s["announced"] = announced + written
    if failed:
        return {"systemMessage": "dialogue-to-rules：这条没记上——" + "；".join(failed)}
    return None


def start_context(source, session):
    parts = [LIVE_RULES.format(script=SCRIPT)] if teaches() else []
    try:
        tasks = [r for r in d2r.load(False) if r["status"] == "active" and r["scope"] == "task"]
    except (OSError, ValueError, KeyError):
        tasks = []
    if tasks:
        shown = tasks[-TASK_RULES_SHOWN:]
        more = f"（另有 {len(tasks) - len(shown)} 条更早的，用 list 查看）" if len(tasks) > len(shown) else ""
        parts.append("本项目还在生效的任务级规则（这个项目里要遵守）：\n"
                     + "\n".join(d2r.render_rule(r) for r in shown) + more)
    if source != "compact":  # after a compact the user has already been told
        try:
            d2r.migrate_cands(host())
            notices = d2r.take_notices()
        except (OSError, ValueError, KeyError) as e:
            log(f"告知没取成：{type(e).__name__}: {e}")
            notices = []
        if notices:
            items = "；".join(n["brief"] + ("（所有项目）" if n.get("scope") == "global" else "") for n in notices)
            parts.append(
                f"上次你不在的时候，插件自动记下了 {len(notices)} 条，已经生效，用户还不知道：{items}。\n"
                "用户说第一句话时，正常回答他，在这条回复的最后单独一行告诉他：\n"
                f"📌 之前还记下了：{items}。不要就说「撤」\n"
                "只说这一次，不要先于用户开口。")
            with Locked() as st:
                st.session(session)["notices_shown"] = [n["id"] for n in notices]
    return "\n\n".join(parts)


def main():
    if sys.argv[1:] == ["install-notifier"]:
        install_notifier()
        return
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(payload, dict) or not active():
        return
    try:
        os.environ["D2R_PROJECT"] = project_of(payload)  # d2r.py resolves every path from this
        event = payload.get("hook_event_name")
        if event == "UserPromptSubmit":
            ctx = on_prompt(payload)
            if ctx:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                         "additionalContext": ctx}}, ensure_ascii=False))
        elif event == "Stop":
            out = on_stop(payload)
            if out:
                print(json.dumps(out, ensure_ascii=False))
        elif event == "SessionStart":
            ctx = start_context(payload.get("source") or "startup", payload.get("session_id"))
            if ctx:
                    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                         "additionalContext": ctx}}, ensure_ascii=False))
    except Exception as e:  # a hook that raises would show up in the user's terminal
        log(f"钩子出错：{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
