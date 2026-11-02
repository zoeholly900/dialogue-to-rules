#!/usr/bin/env python3
"""UserPromptSubmit hook: spot a user correction, inject the rule-card instructions.

Reads the hook JSON from stdin; on a hit prints
{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": <commands/d2r.md>}}
otherwise prints nothing and exits 0. D2R_OFF=1 turns it off.
Rather miss than nag: a false alarm gets the plugin uninstalled.
"""
import json
import os
import re
import sys

# Each pattern is one kind of correction. Keep them narrow; misses are covered by /d2r.
PATTERNS = [
    ("不是…是", r"(?:^|[。！？!?\n])\s*不是[^。！？!?\n]{1,20}[，,]\s*(?:是|而是|我要|要)"),
    ("别再/不要再", r"(?:别|不要|不许|不准)再"),
    ("我说的是/说过", r"我说的是|我(?:刚才|之前|早就|明明)?说过|(?:上次|之前|刚才)(?:跟你)?(?:说|讲)过|说了多少(?:次|遍)"),
    ("你又", r"你(?:怎么)?又(?:把|在|给|用|忘|改|写|没|删|加|来|犯)"),
    ("我没让", r"我(?:没|又没|并没有?)(?:让|叫|要)你"),
    ("X错了", r"(?:搞|弄|写|理解|改|看|听|记)错了"),
    ("不对", r"(?:^|[。！？!?\n])\s*不对[，,。！!]"),
    ("don't…again", r"\bdon'?t\b[^.?!\n]{0,40}\bagain\b"),
    ("I said/told you", r"\bI (?:said|told you|asked you)\b"),
    ("that's not what", r"\bthat'?s not what\b"),
    ("stop doing", r"\bstop doing\b"),
    ("not what I asked", r"\bnot what I (?:asked|meant|wanted)\b"),
    ("I didn't ask", r"\bI (?:didn'?t|did not) (?:ask|tell) you\b"),
]
COMPILED = [(name, re.compile(p, re.I)) for name, p in PATTERNS]

FALLBACK = ("用户可能在纠正你。先出一张【规则卡】（证据=用户原话、范围默认 task），"
            "问 y/n，再接着干活；y 之后用 d2r.py add 写入。")


def match(text):
    """Return the name of the first pattern that hits, or None."""
    for name, rx in COMPILED:
        if rx.search(text or ""):
            return name
    return None


def instructions():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "commands", "d2r.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return FALLBACK
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()  # drop frontmatter
    # injected text is not a slash command: fill in what Claude Code would have substituted
    return text.replace("${CLAUDE_PLUGIN_ROOT}", root).replace(" $ARGUMENTS", "")


def main():
    if os.environ.get("D2R_OFF") == "1":
        return
    try:
        prompt = json.load(sys.stdin).get("prompt", "")
    except (ValueError, AttributeError):
        return
    if match(prompt):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": instructions()}},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
