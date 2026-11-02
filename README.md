# dialogue-to-rules

你纠过一次，它下次不犯，装上就行。

**v0.1 只做这一件事，欢迎挑。**

这是一个 Claude Code 插件。你在对话里纠正它的那一句话（「别再给我加 emoji 了」「我说的是周报，不是日报」），它会出一张规则卡：写清楚你说了什么、它以后该怎么做，你回 `y`，才写下来。写下来的规则有证据（你的原话）、有范围、能撤销。

不是记忆系统，不抓全量对话。只管一个时刻：你纠错的那一句。

## 装

需要 Claude Code。装法有两种，**目前只有本地路径这一种实测过**（输出原样在 [docs/安装实录_1102.md](docs/安装实录_1102.md)）。

本地路径（先把仓库 clone 下来；在你要用它的项目目录里执行）：

```
claude plugin install dialogue-to-rules --marketplace <本仓库的本地路径> --scope project
```

GitHub（**仓库公开后才可用，还没实测过**）：

```
claude plugin install dialogue-to-rules --marketplace <owner>/<repo> --scope project
```

说明：

- `--scope project` 只在当前目录生效，会往 `.claude/settings.json` 写一行 `enabledPlugins`。换一个项目要用，在那个项目里再装一次。
- 装完开一个新会话就生效，不用配置。
- 卸载：`claude plugin uninstall dialogue-to-rules@dialogue-to-rules --scope project`，再 `claude plugin marketplace remove dialogue-to-rules`。

## 它做什么

1. **认出纠错**。你每发一条消息，一个 hook 用正则看一眼（`hooks/detect.py`，纯标准库）。命中「别再…」「不是…是…」「我说的是」「你又…」「我没让你」，或英文的 `don't … again`、`I said`、`that's not what`、`stop doing` 一类，才会往对话里注入说明；没命中什么都不发生。宁可漏，不吵你。`D2R_OFF=1` 整体关掉。
2. **出一张卡，先出卡再接着干活**：

   ```
   【规则卡】
   证据：「你的原话，一字不改」
   我刚才：做了什么
   以后：当 …（触发）→ 做 … / 不做 …
   范围：task（默认）｜project｜global
   测试：给我 … 时，我应该 …
   y 写入 ／ n 丢弃 ／ p 改 project ／ g 改 global
   ```

   规矩：证据只能是你的原话；「以后」只写这一次纠的那件事，不扩大；范围默认 task，你不说「以后都」就不升级；模型猜的东西要标成猜。
3. **你回 y，才写**。写入只由脚本 `scripts/d2r.py` 做，模型不直接改你的 CLAUDE.md。
   - `task`（默认）：只记在 `.d2r/rules.jsonl`，**不**进任何 md。
   - `project`（回 `p`）：渲染进当前目录 `CLAUDE.md` 里的一个托管块。
   - `global`（回 `g`）：渲染进 `~/.claude/CLAUDE.md` 的托管块。
   - 托管块由 `<!-- d2r:begin … -->` 和 `<!-- d2r:end -->` 两行圈起来，块外的内容脚本一个字不碰；你可以手改块里的字，别删这两行。
4. **能撤销**。规则不物理删，作废只改状态；作废的不再渲染进 md（省 CLAUDE.md 的篇幅），但留在 `.d2r/rules.jsonl` 里。

## 命令

斜杠命令（漏认的时候手动触发，把你刚才的话带上）：

```
/dialogue-to-rules:d2r 你刚才…
```

脚本（`scripts/d2r.py`，纯标准库，托管块里的提示行会写出它的实际路径）：

| 命令 | 做什么 |
|---|---|
| `add` | 从 stdin 读一张卡的 JSON，写入（模型 y 之后调它） |
| `list [--all]` | 看现行规则；`--all` 带上已作废的 |
| `deprecate <id> [--reason 文字]` | 作废一条（不删） |
| `deprecate --all-except r2,r5,r9` | 除了列出的，其余全部作废（不删） |
| `undo` | 撤回上一次写入 |
| `render` | 重建托管块（幂等） |

## 不做什么

- 不做记忆，不做向量库，不抓全量对话。
- 不自动整理、不自动合并规则。
- 不做跨项目的个人 profile。
- 不做过期日期。
- 不联网，不收集任何东西。规则都在你本地的文件里。

## 已知局限

- **中文纠错检测会漏。** 你的话不含上面那些标记（比如「这种判断没有通用定律，每次情况不同」），hook 不会触发，要自己打 `/dialogue-to-rules:d2r <那句话>`。这是有意的：误报一次，你就会把它卸掉。
- **hook 不能交互**，所以 y / n 是模型来问你、脚本来写；模型不听话直接动 CLAUDE.md 的情况，靠「规则只由脚本写入」这条约定兜底，但没有技术上的强制。
- **卡的格式靠模型照办。** 实测里偶尔会多出一行「推断：…」；证据和范围按要求给出，但没有程序校验它是不是你的原话。
- **纠错句后面紧跟的问题，答案在卡后面。** 先出卡再接着干活，是 v0.1 的设计；回复的开头是那张卡。
- **task 级规则不进 md**，只活在这次会话的上下文里；新开会话它不会被读到。要跨会话生效，回 `p`（或 `g`）。
- **Codex 侧还没做。** 脚本有 `--host codex`（渲染到 `AGENTS.md`），但 Codex 的 hook / 说明文没接，v0.1 不保证能用。
- 只在 Claude Code 里、`claude-sonnet-5-5` 上验过。其他模型没测。

## 做过的验证

- 单测：`python3 -m unittest discover -s tests`，20 条。
- 检测样本：20 条正样本命中 20、20 条负样本命中 0（负样本专放易误报的：「别客气、别绕弯子」「不要用图表」「报错了」「又快又好」等），见 [docs/检测样本表_v0.1.md](docs/检测样本表_v0.1.md)。
- 五条行为测试（周末规则边界、单次判断不升级、批量作废、推断不当用户说过、纠正不过度泛化），在装好插件的空目录里跑过一轮：周末规则、单次判断、批量作废、纠正不过度泛化这 4 条过。「推断不当用户说过」（不替用户的角色写内心）**不稳定**：同一输入跑 4 次，3 次过 1 次不过，这条里插件没介入（没有纠错，hook 不触发），v0.1 管不到，不记成过。完整输入输出在 [docs/测试记录_v0.1.md](docs/测试记录_v0.1.md)，测试集在 [fixtures/testset_v0.1.md](fixtures/testset_v0.1.md)，合成案例在 [fixtures/synthetic/](fixtures/synthetic/)。

## 反馈

v0.1 只做「你纠错的那一句」这一件事。哪里不对、哪里碍事、缺哪一步，直接挑。
