#!/usr/bin/env python3
"""Print the 1104 hit table (every rule that hits, not just the first) as Markdown.

python3 tests/report_fish_1104.py > docs/自家试用/放宽结果_1104.md
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
import detect  # noqa: E402

# Not scored: made up after the first tuning pass and then used once to tighten it, so not truly unseen.
# Whether each one is a correction is our own call.
HELD_OUT = [
    ("你还记得吗，上周那个文件放哪了", False), ("但是这样会不会太慢吧？", False),
    ("不用急，明天弄就行", False), ("哪有这么好的事，哈哈", False),
    ("我的意思是你先跑一下测试再说", True), ("我想说的是谢谢你", False),
    ("你为什么要用这个库？我好奇", False), ("也不是不行，就是慢", False),
    ("不是吧，这么快就好了，是真的吗", False), ("这个算啥？新功能吗", False),
    ("好的哥们，辛苦了", False), ("不过，今天先到这吧？", False),
    ("但是这个是你写的吧？", False), ("你还记得吗？", False),
    ("不用改了，这样就行", False), ("闭着眼睛都能写，哥们厉害", False),
]


def rules(text):
    strong = [n for n, rx in detect.COMPILED if rx.search(text)]
    weak = [n for n, rx in detect.WEAK_COMPILED if rx.search(text)]
    return strong, weak


def row(i, text, should):
    strong, weak = rules(text)
    fired = detect.match(text) is not None
    mark = ("✅" if fired else "❌漏") if should else ("⚠️误报" if fired else "✅")
    why = "、".join(strong) or ("弱 " + "+".join(weak) if len(weak) >= detect.WEAK_NEEDED else "—")
    if strong and weak:
        why += f"（弱信号 {'+'.join(weak)}）"
    return f"| {i} | {'该' if should else '不该'} | {text.replace('|', '｜')} | {mark} | {why} |", fired


def table(items):
    out = ["| # | 该出卡 | 原话 | 结果 | 中的规则 |", "|---|---|---|---|---|"]
    stats = []
    for i, (text, should) in enumerate(items, 1):
        line, fired = row(i, text, should)
        out.append(line)
        stats.append((should, fired))
    return out, stats


def main():
    with open(os.path.join(ROOT, "fixtures", "real_corrections_1104.jsonl"), encoding="utf-8") as f:
        fish = [(d["text"], d["should_fire"]) for d in map(json.loads, filter(str.strip, f))]
    lines, stats = table(fish)
    x = sum(s and f for s, f in stats)
    y = sum(not s and f for s, f in stats)
    print("# 1104 放宽后检测结果（真实纠错句）\n")
    print("> 由 `tests/report_fish_1104.py` 生成；真源 `fixtures/real_corrections_1104.jsonl`。"
          "规则就是照这 26 句调的，所以这张表是「考过的题」，不代表新话的命中率。\n")
    print(f"**该出卡 {x}/{sum(s for s, _ in stats)} 中，不该出卡 {y}/{sum(not s for s, _ in stats)} 误报。**\n")
    print("\n".join(lines))
    with open(os.path.join(ROOT, "fixtures", "real_corrections_1104_heldout.jsonl"), encoding="utf-8") as f:
        six = [(d["text"], d["should_fire"]) for d in map(json.loads, filter(str.strip, f))]
    sl, ss = table(six)
    print(f"\n## 考题外的真实纠错句（1104 补测）\n\n正则 {sum(f for _, f in ss)}/{len(ss)} 中——"
          "12/12 是背题背出来的。所以默认改成 nudge 模式：正则没中的每条消息都带一句短提醒，"
          "让模型自己判断要不要出卡；这几句弹不弹卡要新开窗实测。\n")
    print("\n".join(sl))
    hl, hs = table(HELD_OUT)
    hx = sum(not s and f for s, f in hs)
    print(f"\n## 考题外自拟句（不计分，看规则会不会伸太长）\n\n第一轮调完后另编的 {len(hs)} 句，拿来收紧过一次，"
          f"所以也不算全新的题；是否算纠正是我们自己判的。误报 {hx}/{sum(not s for s, _ in hs)}。\n")
    print("\n".join(hl))


if __name__ == "__main__":
    main()
