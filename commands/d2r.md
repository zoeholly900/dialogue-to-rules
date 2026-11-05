---
description: 把用户刚才的纠错写成一张规则卡，问 y/n，再交给 d2r.py 写入
argument-hint: "[你刚才…]"
---

用户刚才在纠正你（或用户打了 `/d2r $ARGUMENTS` 手动触发）。照下面做，**先出卡，再接着把手上的活干完**，不要停下来等。

1. 在回复开头出一张规则卡，格式一字不改：

```
【规则卡·还没写入】
证据：「用户原话，一字不改」
我刚才：一句，做了什么
以后：当 …（触发）→ 做 … / 不做 …
范围：task（默认）｜project｜global
测试：给我 … 时，我应该 …
回 y 才写入 ／ n 丢弃 ／ p 改 project ／ g 改 global
```

卡要整张出，七行一行不少；不许缩成一句"我记成了一条规则"，那读着像已经写了，其实还没有。

2. 四条规矩：
   - **证据只能是用户原话**，原样摘，不改写、不概括。你猜的用户意图不是证据，不许写进证据栏。
   - **「以后」只写这一次纠的那件事**，不扩大。用户说「别绕弯子」，规则是「直接给判断」，不是「回答要短」。
   - **范围默认 task**。用户没说「以后都」「每次」「所有项目」，就不升 project / global。
   - 卡里写的推断要标成推断（「我猜…，对吗」），不能写成用户说过。

3. 用户回 y / p / g 之后，**只用脚本写入**，不要自己改 CLAUDE.md / AGENTS.md：

```
echo '<卡的 JSON>' | python3 "${CLAUDE_PLUGIN_ROOT}/scripts/d2r.py" add
```

JSON 字段：`{"trigger": "...", "action": "...", "prohibit": "...", "scope": "task|project|global", "evidence": {"quote": "用户原话"}, "supersedes": []}`。action 和 prohibit 至少一个。p 就把 scope 写 project，g 写 global。用户回 n 就什么都不写。

4. 用户说某条规则作废：`d2r.py deprecate <id>`；「除了 r2、r5 其余都作废」：`d2r.py deprecate --all-except r2,r5`；写错了要撤回：`d2r.py undo`；看现有规则：`d2r.py list --all`。作废不删，只改状态。
