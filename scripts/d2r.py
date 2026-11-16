#!/usr/bin/env python3
"""dialogue-to-rules: the only thing that writes rule files.

Usage:
  d2r.py add [--host claude|codex]            < card.json
  d2r.py deprecate <id> [--reason TEXT]
  d2r.py deprecate --all-except r2,r5,r9 [--reason TEXT]
  d2r.py list [--all]
  d2r.py undo
  d2r.py render [--host claude|codex]
  d2r.py candidates
  d2r.py promote <c001> [--scope task|project|global]
  d2r.py drop <c001>
  d2r.py retract                              the user said 撤: deprecate the latest rule written here
  d2r.py migrate                              turn every queued candidate into a rule (default-on)
  d2r.py book                                 rebuild the rule book (规矩本.html) and print its path

Storage (source of truth):
  task / project rules -> <project>/.d2r/rules.jsonl   ids r001, r002, ...
  global rules         -> ~/.d2r/rules.jsonl           ids g001, g002, ...
Candidates (the old y/n queue; since 0.3 nothing waits there, `migrate` turns it into rules):
  <project>/.d2r/候选.jsonl      ids c001, c002, ...
  <project>/.d2r/候选_已丢.jsonl  quotes the user said n to, so they are not proposed again
Default-on bookkeeping (0.3):
  <project>/.d2r/最近写入.jsonl   ids written from this project, newest last; `retract` walks it backwards
  <project>/.d2r/待告知.jsonl     rules that went live without the user seeing them (sweep, migrate);
                                 hooks/live.py tells the user in one line next window
The rule book (0.3.2): ~/.d2r/规矩本.html, one page with every rule of every project this machine has
written to (~/.d2r/projects.json) plus the global ones, newest first, active and retracted. Rebuilt on
every write; ~/.d2r/规矩本_跳转/<anchor>.html are one-line pages that open the book at that rule
(a notification can open a file but a file URL loses its #fragment on the way).
Rendering: active project rules go into a managed block in <project>/CLAUDE.md
(Codex: AGENTS.md); active global rules into ~/.claude/CLAUDE.md (Codex:
~/.codex/AGENTS.md). Task rules and deprecated rules are never rendered.
Text outside the managed block is never touched.

Env: D2R_PROJECT (default: cwd), D2R_HOME (default: ~), D2R_HOST (default: claude).
Standard library only.
"""
import argparse
import datetime
import hashlib
import html
import json
import os
import re
import sys

BEGIN = "<!-- d2r:begin  由 dialogue-to-rules 维护；手改可以，别删这两行标记 -->"
END = "<!-- d2r:end -->"
BLOCK_RE = re.compile(r"<!-- d2r:begin[^\n]*-->.*?<!-- d2r:end -->", re.S)
SCOPES = ("task", "project", "global")
HOSTS = ("claude", "codex")


# ---------- paths ----------

def project_dir():
    return os.path.abspath(os.environ.get("D2R_PROJECT") or os.getcwd())


def home_dir():
    return os.path.abspath(os.environ.get("D2R_HOME") or os.path.expanduser("~"))


def store_path(is_global):
    base = home_dir() if is_global else project_dir()
    return os.path.join(base, ".d2r", "rules.jsonl")


def cand_path():
    return os.path.join(project_dir(), ".d2r", "候选.jsonl")


def dropped_path():
    return os.path.join(project_dir(), ".d2r", "候选_已丢.jsonl")


def recent_path():
    return os.path.join(project_dir(), ".d2r", "最近写入.jsonl")


def notices_path():
    return os.path.join(project_dir(), ".d2r", "待告知.jsonl")


def last_op_path():
    return os.path.join(project_dir(), ".d2r", "last_op.json")


def book_path():
    return os.path.join(home_dir(), ".d2r", "规矩本.html")


def jump_path(anchor_id):
    return os.path.join(home_dir(), ".d2r", "规矩本_跳转", anchor_id + ".html")


def projects_path():
    return os.path.join(home_dir(), ".d2r", "projects.json")


def md_path(is_global, host):
    if is_global:
        sub, name = (".claude", "CLAUDE.md") if host == "claude" else (".codex", "AGENTS.md")
        return os.path.join(home_dir(), sub, name)
    return os.path.join(project_dir(), "CLAUDE.md" if host == "claude" else "AGENTS.md")


# ---------- io ----------

def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".d2r-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_text(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def load(is_global):
    text = read_text(store_path(is_global)) or ""
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def save(is_global, rules):
    atomic_write(store_path(is_global),
                 "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rules))


def load_jsonl(path):
    text = read_text(path) or ""
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def append_jsonl(path, items):
    if items:
        atomic_write(path, (read_text(path) or "")
                     + "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items))


def load_cands():
    return load_jsonl(cand_path())


def save_cands(cands):
    path = cand_path()
    if not cands:
        if os.path.exists(path):
            os.remove(path)
        return
    atomic_write(path, "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cands))


def snapshot(is_global):
    """Remember the store as it was before this write, so `undo` can put it back."""
    atomic_write(last_op_path(), json.dumps(
        {"global": is_global, "before": read_text(store_path(is_global))}, ensure_ascii=False))


# ---------- rendering ----------

def one_line(s):
    return " ".join(str(s or "").split())


def render_rule(r):
    what = []
    if r.get("action"):
        what.append("做 " + one_line(r["action"]))
    if r.get("prohibit"):
        what.append("不做 " + one_line(r["prohibit"]))
    return "- [{}] 当{} → {} ｜证据「{}」｜范围 {}".format(
        r["id"], one_line(r["trigger"]), " / ".join(what),
        one_line(r["evidence"]["quote"]), r["scope"])


def render_block(rules):
    # a visible line, not an HTML comment, so the model reading CLAUDE.md sees how to change the block
    hint = ("> 这块由 dialogue-to-rules 维护。改规则用 `python3 \"{}\"`：list --all 查看，deprecate <id> 或 "
            "deprecate --all-except r2,r5 作废（不删），undo 撤回上一次。".format(os.path.abspath(__file__)))
    lines = [render_rule(r) for r in rules]
    return "\n".join([BEGIN, hint] + lines + [END])


def render_file(path, rules):
    """Rewrite only the managed block. No block and nothing to show -> leave the file alone."""
    text = read_text(path)
    block = render_block(rules)
    if text is not None and BLOCK_RE.search(text):
        new = BLOCK_RE.sub(lambda m: block, text, count=1)
    elif not rules:
        return
    elif text is None or text == "":
        new = block + "\n"
    else:
        new = text + ("" if text.endswith("\n") else "\n") + "\n" + block + "\n"
    if new != text:
        atomic_write(path, new)


def render(host):
    proj = [r for r in load(False) if r["status"] == "active" and r["scope"] == "project"]
    glob = [r for r in load(True) if r["status"] == "active"]
    render_file(md_path(False, host), proj)
    render_file(md_path(True, host), glob)
    try:  # the book is for reading; a failure here must never cost a rule
        render_book()
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"规矩本没更新：{type(e).__name__}: {e}", file=sys.stderr)


# ---------- the rule book (one HTML page) ----------

def anchor(r, project=None):
    """Stable id of a rule on the page: g008, or p<6 hex of the project path>-r003."""
    if r["id"].startswith("g"):
        return r["id"]
    return "p{}-{}".format(hashlib.sha1((project or project_dir()).encode("utf-8")).hexdigest()[:6], r["id"])


def known_projects():
    """Every project this machine has written rules for, the current one included (and remembered)."""
    try:
        known = json.loads(read_text(projects_path()) or "[]")
    except ValueError:
        known = []
    here = project_dir()
    if here not in known and os.path.exists(store_path(False)):
        known.append(here)
        atomic_write(projects_path(), json.dumps(known, ensure_ascii=False, indent=0))
    return [p for p in known if os.path.exists(os.path.join(p, ".d2r", "rules.jsonl"))]


def _load_from(path):
    text = read_text(path) or ""
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def book_entries():
    out = [(r, None) for r in load(True)]
    for p in known_projects():
        out += [(r, p) for r in _load_from(os.path.join(p, ".d2r", "rules.jsonl"))]
    out.sort(key=lambda e: e[0].get("added_at") or e[0]["evidence"].get("date") or "", reverse=True)
    return out


BOOK_CSS = """
:root{--bg:#f7f5f0;--card:#fff;--ink:#1d1d1f;--muted:#6e6e73;--line:#e5e2da;--live:#1f7a4d;--live-bg:#e3f4ea;
--gone:#8a8a8e;--gone-bg:#eeeeef;--hit:#fff3c4;--quote:#f2efe8}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#17171a;--card:#222226;--ink:#f2f2f4;
--muted:#a1a1a6;--line:#34343a;--live:#5fd396;--live-bg:#1d3a2b;--gone:#8e8e93;--gone-bg:#2c2c30;--hit:#4a4020;--quote:#2b2b30}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:18px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC",sans-serif}
main{max-width:760px;margin:0 auto;padding:28px 16px 64px}
h1{font-size:30px;margin:0 0 4px}.sub{color:var(--muted);font-size:15px;margin:0 0 20px}
.bar{display:flex;gap:8px;margin:0 0 18px}.bar label{font-size:15px;color:var(--muted);cursor:pointer}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:0 0 14px;
scroll-margin-top:16px}
.card:target,.card.hit{background:var(--hit)}
.rule{font-size:21px;font-weight:600;margin:6px 0 10px}
.quote{background:var(--quote);border-radius:8px;padding:8px 12px;margin:0 0 10px;font-size:16px}
.meta{color:var(--muted);font-size:14px}.how{font-size:15px;margin-top:8px}
.tag{display:inline-block;font-size:13px;font-weight:600;border-radius:999px;padding:2px 10px;margin-right:6px}
.on{color:var(--live);background:var(--live-bg)}.off{color:var(--gone);background:var(--gone-bg)}
.card.gone .rule{color:var(--gone);text-decoration:line-through}
body.hide-gone .card.gone{display:none}
code{font-size:14px}
"""


def book_html():
    entries = book_entries()
    live = sum(1 for r, _ in entries if r["status"] == "active")
    cards = []
    for r, p in entries:
        where = "所有项目" if p is None else "项目 " + os.path.basename(p.rstrip("/"))
        on = r["status"] == "active"
        when = (r.get("added_at") or r["evidence"].get("date") or "")[:19].replace("T", " ")
        if on:
            tag = '<span class="tag on">生效</span>'
            say = ("在任意一个窗里说「把 {id} 撤了」" if p is None else "在 {name} 的窗里说「把 {id} 撤了」").format(
                id=r["id"], name=html.escape(os.path.basename(p.rstrip("/")) if p else ""))
            how = f"想撤：刚记下的那一轮直接回「撤」；以后{say}。"
        else:
            why = r.get("reason") or ""
            tag = f'<span class="tag off">已撤 {html.escape(r.get("deprecated_at") or "")}</span>'
            how = "已撤，不再生效" + (f"（{html.escape(why)}）" if why else "") + "。"
        cards.append(
            f'<section class="card{"" if on else " gone"}" id="{anchor(r, p)}">'
            f'<div>{tag}<span class="meta">{html.escape(r["id"])} · {html.escape(where)}</span></div>'
            f'<div class="rule">{html.escape(brief(r))}</div>'
            f'<div class="quote">证据：「{html.escape(one_line(r["evidence"].get("quote")))}」</div>'
            f'<div class="meta">记下于 {html.escape(when)}'
            + (f' · <span title="{html.escape(p)}">{html.escape(p)}</span>' if p else "") + "</div>"
            f'<div class="how">{how}</div></section>')
    empty = '<p class="sub">还没有记下任何规矩。</p>' if not entries else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>规矩本</title><style>{BOOK_CSS}</style></head>
<body><main>
<h1>规矩本</h1>
<p class="sub">dialogue-to-rules 替你记下的规矩，最新的在最上面。生效 {live} 条，已撤 {len(entries) - live} 条。更新于 {now().replace("T", " ")}</p>
<div class="bar"><label><input type="checkbox" id="hide"> 只看生效的</label></div>
{empty}{"".join(cards)}
</main>
<script>
var box=document.getElementById("hide");
try{{box.checked=localStorage.getItem("d2r-hide-gone")==="1"}}catch(e){{}}
function apply(){{document.body.classList.toggle("hide-gone",box.checked)}}
box.addEventListener("change",function(){{apply();try{{localStorage.setItem("d2r-hide-gone",box.checked?"1":"0")}}catch(e){{}}}});
apply();
if(!location.hash){{var c=document.querySelector(".card");if(c)c.classList.add("hit")}}
</script>
</body></html>
"""


def render_book():
    atomic_write(book_path(), book_html())
    return book_path()


def jump_to(r, project=None):
    """A one-line page that opens the book at this rule. -> its path."""
    a = anchor(r, None if r["id"].startswith("g") else (project or project_dir()))
    target = "../规矩本.html#" + a
    atomic_write(jump_path(a), f'<!doctype html><meta charset="utf-8"><title>规矩本</title>'
                               f'<meta http-equiv="refresh" content="0;url={target}">'
                               f'<script>location.replace("{target}")</script>'
                               f'<a href="{target}">打开规矩本</a>\n')
    return jump_path(a)


# ---------- commands ----------

def today():
    return datetime.date.today().isoformat()


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def brief(r):
    """当X就Y, the one line the user reads."""
    t, a, p = one_line(r.get("trigger")), one_line(r.get("action")), one_line(r.get("prohibit"))
    if a:
        return f"当{t}就{a}" + (f"，不要{p}" if p else "")
    return f"当{t}不要{p}"


def norm_id(s):
    """r2 / R002 / r002 -> r002 (same for g)."""
    m = re.fullmatch(r"\s*([rRgG])0*(\d+)\s*", s)
    if not m:
        raise SystemExit(f"不认识的规则 id：{s!r}")
    return f"{m.group(1).lower()}{int(m.group(2)):03d}"


def next_id(rules, prefix):
    nums = [int(r["id"][1:]) for r in rules if r["id"].startswith(prefix)]
    return f"{prefix}{(max(nums) if nums else 0) + 1:03d}"


def add_rule(card, host, source="manual", session=None):
    """Validate a card, write it to the store and re-render. Returns the new rule."""
    scope = card.get("scope") or "task"
    if scope not in SCOPES:
        raise SystemExit(f"scope 只能是 {'/'.join(SCOPES)}")
    ev = card.get("evidence") or {}
    if isinstance(ev, str):
        ev = {"quote": ev}
    if not one_line(ev.get("quote")):
        raise SystemExit("缺证据：evidence.quote 必须是用户原话")
    if not one_line(card.get("trigger")):
        raise SystemExit("缺 trigger")
    if not (one_line(card.get("action")) or one_line(card.get("prohibit"))):
        raise SystemExit("action 和 prohibit 至少要有一个")
    is_global = scope == "global"
    rules = load(is_global)
    supersedes = [norm_id(x) for x in (card.get("supersedes") or [])]
    rule = {
        "id": next_id(rules, "g" if is_global else "r"),
        "status": "active",
        "scope": scope,
        "trigger": card["trigger"].strip(),
        "action": (card.get("action") or "").strip(),
        "prohibit": (card.get("prohibit") or "").strip(),
        "evidence": {"quote": ev["quote"], "date": ev.get("date") or today(),
                     "host": ev.get("host") or host},
        "supersedes": supersedes,
        "deprecated_at": None,
        "reason": "",
        "version": 1,
        "added_at": datetime.datetime.now().isoformat(timespec="milliseconds"),  # orders the rule book
        "source": source,
    }
    snapshot(is_global)
    for r in rules:  # a newer rule that supersedes an old one retires it, never deletes it
        if r["id"] in supersedes and r["status"] == "active":
            r.update(status="deprecated", deprecated_at=today(), reason=f"superseded by {rule['id']}")
    rules.append(rule)
    save(is_global, rules)
    append_jsonl(recent_path(), [{"id": rule["id"], "at": rule["added_at"], "session": session}])
    render(host)
    return rule


def apply_auto(cards, host, source, session=None):
    """Default-on: write each card as a rule with no question asked, and queue a one-line notice so the
    user hears about it next window. task is not a default any more: it falls to project."""
    added = []
    for card in cards:
        card = dict(card)
        if card.get("scope") not in ("project", "global"):
            card["scope"] = "project"
        added.append(add_rule(card, host, source=source, session=session))
    append_jsonl(notices_path(), [{"id": r["id"], "brief": brief(r), "scope": r["scope"]} for r in added])
    return added


def migrate_cands(host):
    """Everything still waiting for a y/n in the old queue goes live (the user was never going to answer)."""
    cands = load_cands()
    if not cands:
        return []
    cards = []
    for c in cands:
        card = {k: c.get(k) for k in ("trigger", "action", "prohibit", "scope", "evidence")}
        card["evidence"] = dict(c["evidence"], date=c.get("date") or today())
        cards.append(card)
    added = apply_auto(cards, host, source="candidate")
    save_cands([])
    return added


def take_notices():
    """The notices not yet shown, still-active rules only; the file is cleared."""
    path = notices_path()
    items = load_jsonl(path)
    if os.path.exists(path):
        os.remove(path)
    alive = {r["id"] for g in (False, True) for r in load(g) if r["status"] == "active"}
    return [n for n in items if n.get("id") in alive]


def find_rule(rid):
    is_global = rid.startswith("g")
    rules = load(is_global)
    return is_global, rules, next((r for r in rules if r["id"] == rid), None)


def retract(host, among=None, reason="用户说撤"):
    """Deprecate the newest still-active rule written from this project (or the newest of `among`).
    Never deletes; `undo` brings it back. -> the rule, or None when there is nothing to take back."""
    ids = list(among) if among is not None else [e["id"] for e in load_jsonl(recent_path())]
    for rid in reversed(ids):
        is_global, rules, r = find_rule(rid)
        if r is None or r["status"] != "active":
            continue
        snapshot(is_global)
        r.update(status="deprecated", deprecated_at=today(), reason=reason)
        save(is_global, rules)
        render(host)
        return r
    return None


def print_added(rule):
    print(f"{rule['id']} 已写入（范围 {rule['scope']}）"
          + ("；task 级只记在 .d2r/rules.jsonl，不进 md" if rule["scope"] == "task" else ""))


def cmd_add(args):
    try:
        card = json.load(sys.stdin)
    except ValueError as e:
        raise SystemExit(f"stdin 不是合法 JSON：{e}")
    print_added(add_rule(card, args.host))


def norm_cand_id(s):
    m = re.fullmatch(r"\s*[cC]0*(\d+)\s*", s)
    if not m:
        raise SystemExit(f"不认识的候选 id：{s!r}")
    return f"c{int(m.group(1)):03d}"


def find_cand(cid):
    cid = norm_cand_id(cid)
    cands = load_cands()
    hit = [c for c in cands if c["id"] == cid]
    if not hit:
        raise SystemExit(f"没有候选 {cid}")
    return hit[0], [c for c in cands if c["id"] != cid]


def cmd_candidates(args):
    for c in load_cands():
        print("{} ｜当{} → {} ｜证据「{}」｜建议范围 {}".format(
            c["id"], one_line(c["trigger"]),
            " / ".join(([f"做 {one_line(c['action'])}"] if c.get("action") else [])
                       + ([f"不做 {one_line(c['prohibit'])}"] if c.get("prohibit") else [])),
            one_line(c["evidence"]["quote"]), c.get("scope", "task")))


def cmd_promote(args):
    """y / p / g on a candidate: write it as a rule, then clear it from the queue."""
    cand, rest = find_cand(args.id)
    card = {k: cand.get(k) for k in ("trigger", "action", "prohibit", "scope", "evidence")}
    card["evidence"] = dict(cand["evidence"], date=cand.get("date") or today())
    if args.scope:
        card["scope"] = args.scope
    print_added(add_rule(card, args.host, source="candidate"))
    save_cands(rest)


def cmd_drop(args):
    """n on a candidate: forget it, and remember the quote so it is not proposed again."""
    cand, rest = find_cand(args.id)
    save_cands(rest)
    path = dropped_path()
    old = read_text(path) or ""
    atomic_write(path, old + json.dumps({"quote": cand["evidence"]["quote"], "date": today()},
                                        ensure_ascii=False) + "\n")
    print(f"{cand['id']} 已丢弃，没写任何规则")


def cmd_retract(args):
    r = retract(args.host)
    if r is None:
        raise SystemExit("这个项目里没有可以撤的规则")
    print(f"已撤 {r['id']}：{brief(r)}（不删，undo 能恢复）")


def cmd_migrate(args):
    added = migrate_cands(args.host)
    for r in added:
        print(f"{r['id']} 已生效（范围 {r['scope']}）：{brief(r)}")
    if not added:
        print("候选池是空的，没动")


def cmd_deprecate(args):
    if bool(args.id) == bool(args.all_except is not None):
        raise SystemExit("deprecate 要么给一个 id，要么给 --all-except，二选一")
    if args.id:
        rid = norm_id(args.id)
        is_global = rid.startswith("g")
        rules = load(is_global)
        hit = [r for r in rules if r["id"] == rid]
        if not hit:
            raise SystemExit(f"没有 {rid}")
        if hit[0]["status"] == "deprecated":
            print(f"{rid} 已经是 deprecated，没改")
            return
        targets = hit
    else:
        keep = {norm_id(x) for x in args.all_except.split(",") if x.strip()}
        is_global = args.global_store
        rules = load(is_global)
        missing = keep - {r["id"] for r in rules}
        if missing:
            raise SystemExit(f"--all-except 里有不存在的 id：{', '.join(sorted(missing))}；没改任何东西")
        targets = [r for r in rules if r["status"] == "active" and r["id"] not in keep]
    snapshot(is_global)
    for r in targets:
        r.update(status="deprecated", deprecated_at=today(), reason=args.reason or "")
    save(is_global, rules)
    render(args.host)
    print("已作废：" + (", ".join(r["id"] for r in targets) or "（没有要作废的）"))


def cmd_list(args):
    for is_global in (False, True):
        for r in load(is_global):
            if r["status"] != "active" and not args.all:
                continue
            tail = f"  [deprecated {r['deprecated_at']}{'：' + r['reason'] if r['reason'] else ''}]" \
                if r["status"] == "deprecated" else ""
            print(render_rule(r) + tail)


def cmd_undo(args):
    text = read_text(last_op_path())
    if text is None:
        raise SystemExit("没有可撤回的上一次写入")
    op = json.loads(text)
    path = store_path(op["global"])
    if op["before"] is None:
        if os.path.exists(path):
            os.remove(path)
    else:
        atomic_write(path, op["before"])
    os.remove(last_op_path())
    render(args.host)
    print("已撤回上一次写入")


def cmd_book(args):
    print(render_book())


def cmd_render(args):
    render(args.host)
    print("已重建托管块")


def main(argv=None):
    p = argparse.ArgumentParser(prog="d2r.py", description="dialogue-to-rules rule store")
    p.add_argument("--host", choices=HOSTS, default=os.environ.get("D2R_HOST") or "claude")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add")
    d = sub.add_parser("deprecate")
    d.add_argument("id", nargs="?")
    d.add_argument("--all-except", dest="all_except")
    d.add_argument("--global", dest="global_store", action="store_true",
                   help="--all-except 作用在全局规则上（默认是本项目）")
    d.add_argument("--reason", default="")
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    sub.add_parser("undo")
    sub.add_parser("render")
    sub.add_parser("candidates")
    pr = sub.add_parser("promote")
    pr.add_argument("id")
    pr.add_argument("--scope", choices=SCOPES, help="不给就用候选卡上的范围；回 p 给 project，回 g 给 global")
    dr = sub.add_parser("drop")
    dr.add_argument("id")
    sub.add_parser("retract")
    sub.add_parser("migrate")
    sub.add_parser("book")
    args = p.parse_args(argv)
    {"add": cmd_add, "deprecate": cmd_deprecate, "list": cmd_list,
     "undo": cmd_undo, "render": cmd_render, "candidates": cmd_candidates,
     "promote": cmd_promote, "drop": cmd_drop, "retract": cmd_retract,
     "migrate": cmd_migrate, "book": cmd_book}[args.cmd](args)


if __name__ == "__main__":
    main()
