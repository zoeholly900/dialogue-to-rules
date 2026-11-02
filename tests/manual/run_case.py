#!/usr/bin/env python3
"""Drive one multi-turn `claude -p` session in a directory and record what happened.

Usage: run_case.py <case.json> <workdir> <out.md>
case.json: {"model": "...", "setup": {"CLAUDE.md": "...", ".d2r/rules.jsonl": "..."},
            "turns": ["first prompt", "y", ...], "files_to_show": ["CLAUDE.md", ".d2r/rules.jsonl"]}
The workdir must already have the plugin installed (project scope). Setup files are written fresh.
This is a helper for the manual test run (fixtures/testset_v0.1.md); it does not judge pass/fail.
"""
import datetime
import json
import os
import subprocess
import sys


def main():
    case_path, work, out = sys.argv[1:4]
    case = json.load(open(case_path, encoding="utf-8"))
    for rel in (".d2r/rules.jsonl", ".d2r/last_op.json", "CLAUDE.md", "AGENTS.md"):
        p = os.path.join(work, rel)
        if os.path.exists(p):
            os.remove(p)
    for rel, text in (case.get("setup") or {}).items():
        p = os.path.join(work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    model = case.get("model", "claude-sonnet-5-5")
    # global rules go to a sandbox home, never the tester's real ~/.claude/CLAUDE.md
    sandbox = os.path.join(os.path.dirname(os.path.abspath(work)), os.path.basename(work) + "_home")
    if os.path.isdir(sandbox):
        import shutil
        shutil.rmtree(sandbox)
    os.makedirs(sandbox)
    env = {**os.environ, "D2R_HOME": sandbox}
    sid, log = None, []
    for i, prompt in enumerate(case["turns"], 1):
        cmd = ["claude", "-p", "--model", model, "--output-format", "json",
               "--allowedTools", "Bash(python3:*)", "Read"]
        if sid:
            cmd += ["--resume", sid]
        # prompt goes in on stdin: --allowedTools is variadic and would swallow a trailing argument
        r = subprocess.run(cmd, cwd=work, input=prompt, capture_output=True, text=True, timeout=600, env=env)
        try:
            data = json.loads(r.stdout)
            sid = data.get("session_id") or sid
            reply = data.get("result", "")
        except ValueError:
            reply = "（非 JSON 输出）\n" + r.stdout + r.stderr
        log.append((prompt, reply))
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"- 模型：{model}\n- 时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n\n")
        for i, (p, rep) in enumerate(log, 1):
            f.write(f"**第{i}轮 用户**：\n\n```\n{p}\n```\n\n**回复原文**：\n\n```\n{rep.strip()}\n```\n\n")
        for rel in case.get("files_to_show") or []:
            p = os.path.join(sandbox, rel[2:]) if rel.startswith("~/") else os.path.join(work, rel)
            body = open(p, encoding="utf-8").read() if os.path.exists(p) else "（不存在）"
            f.write(f"**跑完后的 `{rel}`**：\n\n```\n{body.rstrip()}\n```\n\n")
    print(out)


if __name__ == "__main__":
    main()
