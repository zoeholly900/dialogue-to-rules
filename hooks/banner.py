#!/usr/bin/env python3
"""System notifications for dialogue-to-rules: tell the user a rule was written or taken back, without
interrupting anything. Never waits inside a hook, never raises, never a dialog that must be answered.

Best first, each one falls back to the next:
  alerter            (macOS) a banner with two buttons: 「撤」 deprecates that rule right there and shows
                     「已撤：…」; 「看看」 or a click on the banner opens the rule book at that rule; no click:
                     it goes away after TIMEOUT seconds and the rule simply stays. Someone has to wait for
                     the click, so a detached `banner.py wait` process does, never the Stop hook.
  terminal-notifier  (macOS) no buttons; a click opens the rule book at that rule.
  osascript          (macOS) can be seen, not clicked; ends with 「看全部：说「规矩本」」.
  notify-send        (Linux) the same, if it is there. Otherwise nothing.
alerter and terminal-notifier are fetched once, in the background, from their official GitHub releases
into <plugin>/vendor/ (no brew, nothing outside the plugin's folder), checked against the sha256 GitHub
publishes for the asset. A failed fetch is tried again a day later; until then the next one down is used.

CLI (used by the plugin itself):
  banner.py install        fetch whatever is missing into vendor/
  banner.py wait <json>    show one alerter banner and act on the click
Env: D2R_NOTIFY=0 off. D2R_NOTIFY_TIMEOUT=<seconds> how long the button banner waits (default 30). D2R_NOTIFY_LOG=<file> (tests): record banners and page opens there instead.
Standard library only.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import d2r  # noqa: E402

NOTIFY_CHARS = 60          # a banner shows about two lines; the full rule is in the rule book
TITLE_CHARS = 24           # the title row is one line; a 0.3.2 title is ≤16, an old 当X就Y gets cut
TITLE_PLUS, TITLE_MINUS = "规矩+1：", "规矩-1："   # who is talking and what happened, before anything else
BOOK_HINT = "\n看全部：说「规矩本」"   # where a banner cannot be clicked open
TIMEOUT = 30               # seconds an alerter banner waits for a click (D2R_NOTIFY_TIMEOUT overrides)
BUTTONS = ("撤", "看看")
CLOSE_LABEL = "好的"
VENDOR = os.path.join(ROOT, "vendor")
STATE = os.path.join(VENDOR, "install.json")
RETRY = 86400              # a failed download is tried again a day later, not on every banner
API = "https://api.github.com/repos/{repo}/releases/{which}"
TOOLS = {  # name -> where its official release lives and what to take out of the zip
    "alerter": {"repo": "vjeantet/alerter", "which": "latest"},
    "terminal-notifier": {"repo": "julienXX/terminal-notifier", "which": "tags/2.0.0"},
}


def log(msg):
    try:
        path = os.path.join(d2r.project_dir(), ".d2r", "live.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{d2r.now()} {d2r.one_line(msg)[:400]}\n")
    except OSError:
        pass


def sink():
    return os.environ.get("D2R_NOTIFY_LOG")


def record(item):
    with open(sink(), "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def spawn(cmd):
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# ---------- finding / fetching the helpers ----------

def tool_path(name):
    """PATH first, then our own copy (a bare binary or inside an .app)."""
    found = shutil.which(name)
    if found:
        return found
    for p in (os.path.join(VENDOR, name), os.path.join(VENDOR, name + ".app", "Contents", "MacOS", name)):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def maybe_install():
    """Something missing on a Mac: fetch it in the background, at most once a day."""
    missing = [n for n in TOOLS if not tool_path(n)]
    st = read_state()
    if not missing or any(time.time() - st.get(n, {}).get("at", 0) < RETRY for n in missing):
        return
    try:
        d2r.atomic_write(STATE, json.dumps(
            {**st, **{n: {"at": time.time(), "ok": False, "err": "正在装"} for n in missing}}, ensure_ascii=False))
        spawn([sys.executable, os.path.abspath(__file__), "install"])
    except OSError as e:
        log(f"通知工具没装成：{e}")


def fetch_json(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "dialogue-to-rules"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def pick_asset(release):
    zips = [a for a in release.get("assets", []) if a.get("name", "").endswith(".zip")]
    if not zips:
        raise ValueError("release 里没有 zip")
    zips.sort(key=lambda a: (("arm64" not in a["name"] and "universal" not in a["name"]),
                             "darwin" not in a["name"]))
    return zips[0]


def install_one(name, release=None, download=None):
    """Official release -> sha256 checked against GitHub's -> unpacked into vendor/ -> proven to run.
    `release` / `download` are for tests. -> state dict."""
    import hashlib
    import urllib.request
    tmp = os.path.join(VENDOR, f".download-{name}")
    out = {"at": time.time(), "ok": False, "err": "", "url": None, "sha256": None}
    try:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        os.makedirs(tmp)
        rel = release or fetch_json(API.format(**TOOLS[name]))
        asset = pick_asset(rel)
        out["url"] = asset["browser_download_url"]
        zpath = os.path.join(tmp, "dl.zip")
        if download:
            download(out["url"], zpath)
        else:
            with urllib.request.urlopen(out["url"], timeout=120) as resp, open(zpath, "wb") as f:
                shutil.copyfileobj(resp, f)
        with open(zpath, "rb") as f:
            out["sha256"] = hashlib.sha256(f.read()).hexdigest()
        want = (asset.get("digest") or "").replace("sha256:", "")
        if want and want != out["sha256"]:
            raise ValueError(f"校验对不上：GitHub 写的 {want}，下到的 {out['sha256']}")
        out["checked"] = bool(want)
        subprocess.run(["ditto", "-x", "-k", zpath, tmp], check=True, timeout=60, capture_output=True)
        hit = None
        for dp, ds, fs in os.walk(tmp):
            if name + ".app" in ds:
                hit = os.path.join(dp, name + ".app")
                break
            if name in fs:
                hit = os.path.join(dp, name)
                break
        if not hit:
            raise ValueError(f"zip 里没找到 {name}")
        dest = os.path.join(VENDOR, os.path.basename(hit))
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        elif os.path.exists(dest):
            os.remove(dest)
        shutil.move(hit, dest)
        if os.path.isfile(dest):
            os.chmod(dest, 0o755)
        exe = tool_path(name)
        if not exe:
            raise OSError("解开了但找不到可执行文件")
        subprocess.run([exe, "-help"], timeout=20, capture_output=True)
        out["ok"] = True
    except Exception as e:  # noqa: BLE001 - any failure means "use the next one down"
        out["err"] = f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def install():
    st = read_state()
    for name in TOOLS:
        if tool_path(name):
            continue
        st[name] = install_one(name)
        log(f"{name} 装好了" if st[name]["ok"] else f"{name} 没装成：{st[name]['err']}")
    d2r.atomic_write(STATE, json.dumps(st, ensure_ascii=False, indent=1))


# ---------- showing ----------

def short(text, n=None):
    n = n or NOTIFY_CHARS
    return text if len(text) <= n else text[:n - 1] + "…"


def channel(buttons, target):
    """-> (via, exe): buttons (alerter), click (terminal-notifier, or alerter without buttons), plain, or None."""
    if sys.platform == "darwin":
        if not (tool_path("alerter") and tool_path("terminal-notifier")):
            maybe_install()
        al, tn = tool_path("alerter"), tool_path("terminal-notifier")
        if target and al and buttons:
            return "buttons", al
        if target and tn:
            return "click", tn
        if target and al:
            return "click", al
        return ("plain", "osascript") if shutil.which("osascript") else (None, None)
    return ("plain", "notify-send") if shutil.which("notify-send") else (None, None)


def notify(title, body=None, subtitle="", rule=None, buttons=False):
    """Fire and forget. With a rule, a click opens the rule book at it; with buttons, 撤 / 看看 as well.
    body None: one line saying what the user can do, fitted to what this banner can do."""
    if os.environ.get("D2R_NOTIFY") == "0":
        return
    try:
        target = d2r.jump_to(rule) if rule else d2r.book_path()
    except (OSError, ValueError, KeyError) as e:
        log(f"规矩本跳转页没写成：{e}")
        target = None
    buttons = bool(buttons and rule)
    try:
        if sink():
            via, exe = ("buttons" if buttons else "click"), None
        else:
            via, exe = channel(buttons, target)
        if via is None:
            return
        spec = {"title": title, "body": body_for(body, via), "subtitle": subtitle, "open": target,
                "buttons": via == "buttons", "rule": rule["id"] if rule else None,
                "project": d2r.project_dir(), "home": d2r.home_dir()}
        if sink():
            record(spec)
        elif os.path.basename(exe) == "alerter":
            spawn([sys.executable, os.path.abspath(__file__), "wait", json.dumps(spec, ensure_ascii=False)])
        elif via == "click":
            spawn([exe, "-title", title, "-subtitle", subtitle, "-message", spec["body"], "-group", "d2r",
                   "-open", pathlib.Path(target).as_uri()])
        elif exe == "osascript":
            # text goes in as argv, so quotes in a rule cannot break the script
            spawn(["osascript", "-e", "on run argv", "-e",
                   "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
                   "-e", "end run", spec["body"], title, subtitle])
        else:
            spawn(["notify-send", "-a", "dialogue-to-rules", title,
                   (subtitle + "\n" if subtitle else "") + spec["body"]])
    except OSError as e:
        log(f"通知没发出：{e}")


def written(r):
    """规矩+1：<the rule> first, then 适用：…; the body only says what the user can do about it."""
    sub = "适用：" + r["applies"] if r.get("applies") else ("格式不全，点开看原话" if r.get("format") else "")
    notify(TITLE_PLUS + short(d2r.brief(r), TITLE_CHARS), None, sub, rule=r, buttons=True)


def retracted(r):
    notify(TITLE_MINUS + short(d2r.brief(r), TITLE_CHARS), "已撤，以后不再照做", rule=r)


def body_for(body, via):
    """body None: say what can be done, in the words that fit what the banner can do."""
    if body is not None:
        return short(body) + (BOOK_HINT if via == "plain" else "")
    return {"buttons": "不要就点「撤」，点「看看」看规矩本", "click": "点开看规矩本，不要就回「撤」"}.get(
        via, "不要就回「撤」" + BOOK_HINT)


def timeout():
    try:
        return max(5, int(os.environ.get("D2R_NOTIFY_TIMEOUT") or TIMEOUT))
    except ValueError:
        return TIMEOUT


def alerter_args(exe, spec):
    """alerter changed its flags over the years (-actions / -closeLabel, then --actions / --close-label):
    ask it which."""
    helptext = ""
    for ask in ("--help", "-help"):
        try:
            out = subprocess.run([exe, ask], capture_output=True, text=True, timeout=10)
            helptext = out.stdout + out.stderr
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "actions" in helptext:
            break
    d = "--" if "--actions" in helptext else "-"
    close = "--close-label" if "--close-label" in helptext else d + "closeLabel"
    args = [exe, d + "title", spec["title"], d + "message", spec["body"], d + "group", "d2r",
            d + "timeout", str(timeout())]
    if spec["subtitle"]:
        args += [d + "subtitle", spec["subtitle"]]
    if spec["buttons"]:
        args += [d + "actions", ",".join(BUTTONS), close, CLOSE_LABEL]
    return args


def open_page(path):
    if sink():
        record({"opened": path})
    else:
        spawn(["open", path])


def act(answer, spec):
    """What alerter printed -> what to do. 撤: deprecate that very rule and say so; 看看 or a click on the
    banner: open the book there; 好的 / timeout / closed: nothing, the rule stays."""
    answer = (answer or "").strip()
    if answer == BUTTONS[0] and spec.get("rule"):
        os.environ["D2R_PROJECT"], os.environ["D2R_HOME"] = spec["project"], spec["home"]
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import live  # the same lock the hooks use
        with live.Locked():
            r = d2r.retract(live.host(), among=[spec["rule"]], reason="用户点了通知上的「撤」")
        if r is not None:
            log(f"撤 {r['id']}（点通知）")
            retracted(r)
        return "撤"
    if answer in (BUTTONS[1], "@CONTENTCLICKED", "@ACTIONCLICKED") and spec.get("open"):
        open_page(spec["open"])
        return "看看"
    return None


def wait(spec):
    exe = tool_path("alerter")
    if not exe:
        return
    try:
        out = subprocess.run(alerter_args(exe, spec), capture_output=True, text=True, timeout=timeout() + 60)
        if out.returncode != 0:
            log(f"alerter 退出码 {out.returncode}：{(out.stderr or out.stdout).strip()[:200]}")
        answer = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        if answer.startswith("{"):  # --json output
            answer = json.loads(answer).get("activationValue") or json.loads(answer).get("activationType") or ""
            answer = {"contentsClicked": "@CONTENTCLICKED"}.get(answer, answer)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log(f"alerter 出错：{e}")
        return
    act(answer, spec)


def main():
    if sys.argv[1:2] == ["install"]:
        install()
    elif sys.argv[1:2] == ["wait"] and len(sys.argv) > 2:
        spec = json.loads(sys.argv[2])
        os.environ["D2R_PROJECT"], os.environ["D2R_HOME"] = spec["project"], spec["home"]
        wait(spec)


if __name__ == "__main__":
    main()
