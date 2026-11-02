#!/usr/bin/env python3
"""dialogue-to-rules: the only thing that writes rule files.

Usage:
  d2r.py add [--host claude|codex]            < card.json
  d2r.py deprecate <id> [--reason TEXT]
  d2r.py deprecate --all-except r2,r5,r9 [--reason TEXT]
  d2r.py list [--all]
  d2r.py undo
  d2r.py render [--host claude|codex]

Storage (source of truth):
  task / project rules -> <project>/.d2r/rules.jsonl   ids r001, r002, ...
  global rules         -> ~/.d2r/rules.jsonl           ids g001, g002, ...
Rendering: active project rules go into a managed block in <project>/CLAUDE.md
(Codex: AGENTS.md); active global rules into ~/.claude/CLAUDE.md (Codex:
~/.codex/AGENTS.md). Task rules and deprecated rules are never rendered.
Text outside the managed block is never touched.

Env: D2R_PROJECT (default: cwd), D2R_HOME (default: ~), D2R_HOST (default: claude).
Standard library only.
"""
import argparse
import datetime
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


def last_op_path():
    return os.path.join(project_dir(), ".d2r", "last_op.json")


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


# ---------- commands ----------

def today():
    return datetime.date.today().isoformat()


def norm_id(s):
    """r2 / R002 / r002 -> r002 (same for g)."""
    m = re.fullmatch(r"\s*([rRgG])0*(\d+)\s*", s)
    if not m:
        raise SystemExit(f"不认识的规则 id：{s!r}")
    return f"{m.group(1).lower()}{int(m.group(2)):03d}"


def next_id(rules, prefix):
    nums = [int(r["id"][1:]) for r in rules if r["id"].startswith(prefix)]
    return f"{prefix}{(max(nums) if nums else 0) + 1:03d}"


def cmd_add(args):
    try:
        card = json.load(sys.stdin)
    except ValueError as e:
        raise SystemExit(f"stdin 不是合法 JSON：{e}")
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
                     "host": ev.get("host") or args.host},
        "supersedes": supersedes,
        "deprecated_at": None,
        "reason": "",
        "version": 1,
    }
    snapshot(is_global)
    for r in rules:  # a newer rule that supersedes an old one retires it, never deletes it
        if r["id"] in supersedes and r["status"] == "active":
            r.update(status="deprecated", deprecated_at=today(), reason=f"superseded by {rule['id']}")
    rules.append(rule)
    save(is_global, rules)
    render(args.host)
    print(f"{rule['id']} 已写入（范围 {scope}）" + ("；task 级只记在 .d2r/rules.jsonl，不进 md" if scope == "task" else ""))


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
    args = p.parse_args(argv)
    {"add": cmd_add, "deprecate": cmd_deprecate, "list": cmd_list,
     "undo": cmd_undo, "render": cmd_render}[args.cmd](args)


if __name__ == "__main__":
    main()
