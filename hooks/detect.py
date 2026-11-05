#!/usr/bin/env python3
"""UserPromptSubmit hook: spot a user correction, inject the rule-card instructions.

Reads the hook JSON from stdin and prints
{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ...}}
D2R_MODE=nudge (default): a regex hit injects all of commands/d2r.md; every other message gets
NUDGE, a one-line reminder that leaves the "is this a correction?" call to the model.
D2R_MODE=regex: a regex hit injects d2r.md, a miss prints nothing. D2R_OFF=1 turns it all off.
Regexes alone missed most real corrections (they only knew the phrasings we tuned them on).
A miss is worse than a nag, but nagging has a ceiling: one narrow pattern fires alone;
vaguer signals (rhetorical tone, "but … right?") fire only when two of them agree.
"""
import json
import os
import re
import sys

# Each pattern is one kind of correction. Keep them narrow; misses are covered by /d2r.
PATTERNS = [
    ("不是…是", r"(?:^|[。！？!?\n，,])\s*(?:也|并|倒|可)?不是(?![吧吗么])[^。！？!?\n]{1,20}[，,]\s*(?:是|而是|我要|要)"),
    ("别再/不要再", r"(?:别|不要|不许|不准)再"),
    ("我说的是/说过", r"我说的是|我(?:刚才|之前|早就|明明|从来)?(?:没|没有|从没|又没)?说过|(?:上次|之前|刚才)(?:跟你)?(?:说|讲)过|说了多少(?:次|遍)"),
    ("你又", r"你(?:怎么)?又(?:把|在|给|用|忘|改|写|没|删|加|来|犯)"),
    ("我没让", r"我(?:没|又没|并没有?)(?:让|叫|要)你"),
    ("X错了", r"(?:搞|弄|写|理解|改|看|听|记)错了"),
    ("不对", r"(?:^|[。！？!?\n])\s*不对[，,。！!]"),
    ("我的意思是", r"我的意思是|我(?:想|要)表达的(?:就|其实)?是|我(?:刚刚|刚才)?(?:只是|就是)(?:举|打)(?:个|了个)?(?:例子|比方)"),
    ("不用X，Y就行", r"不用(?:全|再)?(?:删|改|换|重写|重做|重来|写|动|加|拆|搬|做)[^，,。！？!?\n]{0,8}[，,]\s*[^，,。！？!?\n]{1,12}就(?:行|好|可以)"),
    ("你为什么/你还能/你还记得吗", r"你(?:为什么|凭什么|怎么就)(?:肯定|觉得|认为|确定|非要|就)|你还能比|你(?:还记得|忘了)(?:吗|么|没)(?:[？?]|\s*$)"),
    ("哪有那么多", r"哪有(?:那么|这么)多"),
    ("don't…again", r"\bdon'?t\b[^.?!\n]{0,40}\bagain\b"),
    ("I said/told you", r"\bI (?:said|told you|asked you)\b"),
    ("that's not what", r"\bthat'?s not what\b"),
    ("stop doing", r"\bstop doing\b"),
    ("not what I asked", r"\bnot what I (?:asked|meant|wanted)\b"),
    ("I didn't ask", r"\bI (?:didn'?t|did not) (?:ask|tell) you\b"),
    ("it's not X. also not", r"\b(?:it|that|this)'?s not\b[^?\n]{0,60}\balso not\b"),
]
# Weak signals: each alone is too vague to fire on. Two different ones in one message do.
WEAK = [
    ("但是开头", r"(?:^|[。！？!?\n])\s*(?:但是|可是|不过)[，,]?"),
    ("是…的吧？反问", r"是[^，,。！？!?\n]{1,15}的吧[？?]"),
    ("没X就Y", r"(?:都|还)?没[^，,。！？!?\n]{1,4}就(?:上|做|发|用|改|写|交)"),
    ("闭着眼睛/想当然", r"闭着眼(?:睛)?|想当然|瞎(?:搞|写|改|编)"),
    ("哥们/大哥", r"[啊呀吧][，,]?\s*(?:哥们|大哥|兄弟)|(?:哥们|大哥)[，,。！!]"),
    ("算啥/凭什么", r"算(?:啥|什么)|凭什么|害人害己"),
]
COMPILED = [(name, re.compile(p, re.I)) for name, p in PATTERNS]
WEAK_COMPILED = [(name, re.compile(p, re.I)) for name, p in WEAK]
WEAK_NEEDED = 2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NUDGE = ("先判断用户这句是不是在纠正你、否定你刚做的或给出它要的正解。不是就当没看见，别提这句。"
         "是就在回复开头出整张卡，七行一行不少，然后接着干活：\n"
         "【规则卡·还没写入】\n证据：「用户原话，一字不改」\n我刚才：一句\n以后：当 …→ 做 … / 不做 …\n"
         "范围：task（默认，用户没说'以后都'就不升）\n测试：给我 … 时，我应该 …\n"
         "回 y 才写入 ／ n 丢弃 ／ p 改 project ／ g 改 global\n"
         "用户回 y/p/g 后再读插件根 {root} 的 commands/d2r.md，按它用 scripts/d2r.py add 写入；n 什么都不写。")

FALLBACK = ("用户可能在纠正你。先出一张【规则卡】（证据=用户原话、范围默认 task），"
            "问 y/n，再接着干活；y 之后用 d2r.py add 写入。")


def match(text):
    """Return the name of the first pattern that hits (or "弱:a+b" for weak signals), or None."""
    text = text or ""
    for name, rx in COMPILED:
        if rx.search(text):
            return name
    weak = [name for name, rx in WEAK_COMPILED if rx.search(text)]
    if len(weak) >= WEAK_NEEDED:
        return "弱:" + "+".join(weak)
    return None


def instructions(root=ROOT):
    path = os.path.join(root, "commands", "d2r.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return FALLBACK
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()  # drop frontmatter
    # injected text is not a slash command: fill in what Claude Code would have substituted
    return text.replace("${CLAUDE_PLUGIN_ROOT}", root).replace(" $ARGUMENTS", "")


def context(prompt):
    """What to inject for this prompt under the current D2R_* settings, or None."""
    if os.environ.get("D2R_OFF") == "1":
        return None
    if match(prompt):
        return instructions()
    if os.environ.get("D2R_MODE", "nudge") == "regex":
        return None
    return NUDGE.format(root=ROOT)


def main():
    try:
        prompt = json.load(sys.stdin).get("prompt", "")
    except (ValueError, AttributeError):
        return
    ctx = context(prompt)
    if ctx is None:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": ctx}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
