"""D2R_MODE=live (default): the rule is written in the turn the user corrects, 撤 takes it back.
Hooks run as subprocesses with fake payloads; no real model is called."""
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import banner  # noqa: E402
import d2r  # noqa: E402
import live  # noqa: E402

LIVE = os.path.join(ROOT, "hooks", "live.py")
D2R = os.path.join(ROOT, "scripts", "d2r.py")

CORRECTION = "你又把路径后面带了句号，以后给我路径都单独一行，后面别跟标点"
RULE, WHERE = "给路径单独一行不跟标点", "给用户任何文件或文件夹路径时"
WHY = "她要直接点开，带标点复制就坏"


def line(rule=RULE, where=WHERE, everywhere=False, why=WHY):
    return f"📌 记下了{'（所有项目）' if everywhere else ''}：{rule}｜适用：{where}｜为什么：{why}。不要就说「撤」"


REPLY = "好的，改了：\n/Users/x/a.md\n/Users/x\n\n" + line()


class LiveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = os.path.join(self.tmp.name, "proj")
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.proj)
        os.makedirs(self.home)
        keys = ("D2R_PROJECT", "D2R_HOME", "D2R_HOST", "D2R_MODE", "D2R_OFF", "D2R_SWEEP_CHILD", "CLAUDE_PROJECT_DIR",
                "D2R_NOTIFY", "D2R_NOTIFY_LOG")
        self.env = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        self.banners = os.path.join(self.tmp.name, "banners.jsonl")  # no real banner pops during tests
        os.environ.update(D2R_PROJECT=self.proj, D2R_HOME=self.home, D2R_HOST="claude", D2R_NOTIFY_LOG=self.banners)

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def hook(self, event, session="s1", env=None, **payload):
        p = {"hook_event_name": event, "session_id": session, "cwd": self.proj, **payload}
        r = subprocess.run([sys.executable, LIVE], input=json.dumps(p, ensure_ascii=False),
                           capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=30)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return json.loads(r.stdout) if r.stdout.strip() else None

    def prompt(self, text, **kw):
        out = self.hook("UserPromptSubmit", prompt=text, **kw)
        return out["hookSpecificOutput"]["additionalContext"] if out else None

    def stop(self, reply, **kw):
        return self.hook("Stop", last_assistant_message=reply, stop_hook_active=False, **kw)

    def start(self, source="startup", **kw):
        return self.hook("SessionStart", source=source, **kw)["hookSpecificOutput"]["additionalContext"]

    def md(self, is_global=False):
        path = os.path.join(self.home, ".claude", "CLAUDE.md") if is_global else os.path.join(self.proj, "CLAUDE.md")
        return d2r.read_text(path) or ""

    def active(self, is_global=False):
        return [r for r in d2r.load(is_global) if r["status"] == "active"]

    def turn(self, user, reply, **kw):
        self.prompt(user, **kw)
        return self.stop(reply, **kw)

    def notified(self):
        if not os.path.exists(self.banners):
            return []
        with open(self.banners, encoding="utf-8") as f:
            return [json.loads(line) for line in f]


class ParseTest(unittest.TestCase):
    def test_the_line_and_its_variants(self):
        self.assertEqual(live.marks(REPLY), [(f"{RULE}｜适用：{WHERE}｜为什么：{WHY}", False)])
        self.assertEqual(live.marks("**📌 记下了（所有项目）**：派活先写来头｜适用：派任何活时。不要的话就说「撤」"),
                         [("派活先写来头｜适用：派任何活时", True)])
        self.assertEqual(live.marks("- 📌 记下了：额度<30%只收尾｜适用：开新活前。不要就说'撤'"),
                         [("额度<30%只收尾｜适用：开新活前", False)])

    def test_templates_code_and_other_pins_are_not_rules(self):
        for text in ("📌 记下了：<规矩一句话>｜适用：<什么时候照做>｜为什么：<防什么问题、为谁>。不要就说「撤」",
                     "📌 记下了：……｜适用：……。不要就说「撤」",
                     "```\n" + line() + "\n```",
                     "📌 之前还记下了：甲｜适用：乙。不要就说「撤」",
                     "我会在末尾写 " + line()):  # not at the start of a line
            self.assertEqual(live.marks(text), [], text)

    def test_gate_two_parts_within_limits(self):
        card, wrong = live.to_card(f"{RULE}｜适用：{WHERE}｜为什么：{WHY}")
        self.assertIsNone(wrong)
        self.assertEqual((card["title"], card["applies"]), (RULE, WHERE))
        self.assertEqual(card["why"], WHY)
        self.assertEqual(live.to_card("派活先写来头 | 适用: 派任何活时 | 为什么: 免得对方再问。")[0]["applies"], "派任何活时")
        _, wrong = live.to_card("当给路径就单独一行、后面不跟标点")  # the 0.3.1 shape
        self.assertIn("没按三段写", wrong)
        _, wrong = live.to_card("给路径单独一行不跟标点｜适用：给路径时｜不适用：代码块")  # a third part that is not 为什么
        self.assertIn("没按三段写", wrong)
        _, wrong = live.to_card("给路径单独一行不跟标点｜适用：给路径时")  # 0.3.2 两段：少了为什么
        self.assertIn("少了第三段「为什么」", wrong)
        _, wrong = live.to_card("给用户的任何文件路径都要单独一行而且不跟标点｜适用：给路径时｜为什么：要能点开")
        self.assertIn("规矩 22 字，超过 16", wrong)
        _, wrong = live.to_card("给路径单独一行｜适用：给用户任何文件、文件夹、网址、命令输出里的路径时｜为什么：要能点开")
        self.assertIn("适用 24 字，超过 20", wrong)
        _, wrong = live.to_card("给路径单独一行｜适用：给路径时｜为什么：" + "她" * 31)
        self.assertIn("为什么 31 字，超过 30", wrong)

    def test_long_message_evidence_is_the_closest_sentence_verbatim(self):
        msg = "今天先这样。" + "别的事情说了很多很多。" * 20 + "以后给我路径都单独一行，后面别跟标点。" + "收工吧。"
        q = live.best_quote(msg, live.to_card(f"{RULE}｜适用：{WHERE}｜为什么：{WHY}")[0])
        self.assertIn("路径都单独一行", q)
        self.assertIn(q, msg)


class CorrectionTurnTest(LiveBase):
    def test_the_correction_turn_writes_a_project_rule_with_the_users_words(self):
        self.assertIsNone(self.turn(CORRECTION, REPLY))
        r, = self.active()
        self.assertEqual((r["id"], r["scope"], r["source"]), ("r001", "project", "live"))
        self.assertEqual((r["title"], r["applies"]), (RULE, WHERE))
        self.assertNotIn("format", r)
        self.assertEqual(r["evidence"]["quote"], CORRECTION)
        self.assertIn(f"[r001] {RULE} ｜适用 {WHERE} ｜为什么 {WHY} ｜证据", self.md())  # default-on: in CLAUDE.md, nobody answered
        self.assertEqual(self.md(True), "")

    def test_all_projects_goes_global(self):
        self.turn("所有窗都一样，派活先写来头", "好。\n" + line("派活先写清为谁做", "派任何活时", everywhere=True))
        self.assertEqual(self.active(), [])
        self.assertEqual(self.active(True)[0]["id"], "g001")
        self.assertIn("派活先写清为谁做", self.md(True))

    def test_no_line_no_rule_and_no_output(self):
        self.assertIsNone(self.turn("帮我看看这个文件", "看完了，没问题。"))
        self.assertEqual(d2r.load(False), [])

    def test_announced_twice_is_written_once(self):
        self.turn(CORRECTION, REPLY)
        self.stop(REPLY)  # e.g. the Stop hook fired again
        self.turn("又带句号了", REPLY)
        self.assertEqual(len(d2r.load(False)), 1)

    def test_transcript_fallback_when_payload_has_no_message(self):
        tr = os.path.join(self.tmp.name, "t.jsonl")
        with open(tr, "w", encoding="utf-8") as f:
            for rec in ({"type": "user", "message": {"role": "user", "content": CORRECTION}},
                        {"type": "assistant", "message": {"role": "assistant",
                                                          "content": [{"type": "text", "text": REPLY}]}}):
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.hook("Stop", transcript_path=tr)  # no UserPromptSubmit record, no last_assistant_message
        self.assertEqual(self.active()[0]["evidence"]["quote"], CORRECTION)

    def test_a_rule_that_could_not_be_stored_is_reported(self):
        os.makedirs(os.path.join(self.home, ".claude", "CLAUDE.md"))  # a directory: rendering fails
        out = self.turn("以后都这样", "好。\n" + line("先甲后乙", "做丙时", everywhere=True))
        self.assertIn("没记上", out["systemMessage"])

    def test_off_does_nothing(self):
        for env in ({"D2R_OFF": "1"}, {"D2R_SWEEP_CHILD": "1"}):
            self.prompt(CORRECTION, env=env)
            self.assertIsNone(self.stop(REPLY, env=env))
            self.assertIsNone(self.hook("SessionStart", source="startup", env=env))
        self.assertEqual(d2r.load(False), [])

    def test_old_modes_write_the_same_line_but_do_not_teach_it_at_start(self):
        for env in ({"D2R_MODE": "nudge"}, {"D2R_MODE": "regex"}):
            self.assertIsNone(self.hook("SessionStart", source="startup", env=env))  # detect.py tells it per message
        self.turn(CORRECTION, REPLY, env={"D2R_MODE": "regex"})
        self.assertEqual(len(self.active()), 1)
        self.assertIsNotNone(self.prompt("撤", env={"D2R_MODE": "regex"}))

    def test_sweep_mode_keeps_the_live_line(self):
        self.turn(CORRECTION, REPLY, env={"D2R_MODE": "sweep"})
        self.assertEqual(len(self.active()), 1)


class GateTest(LiveBase):
    """The card is a code gate, not a request: 规矩 ≤16 ｜适用：≤20, or it is sent back once."""
    def test_compliant_line_is_written_as_is(self):
        self.assertIsNone(self.turn(CORRECTION, REPLY))
        r, = self.active()
        self.assertEqual((r["title"], r["applies"], r.get("format")), (RULE, WHERE, None))

    def test_missing_part_is_sent_back_and_the_fixed_line_is_written(self):
        out = self.turn(CORRECTION, "好的。\n📌 记下了：当给路径就单独一行、后面不跟标点。不要就说「撤」")
        self.assertEqual(out["decision"], "block")  # the model goes on in this same turn
        self.assertIn("没按三段写", out["reason"])
        self.assertIn("📌 记下了：<规矩一句话>｜适用：<什么时候照做>｜为什么：<防什么问题、为谁>。不要就说「撤」", out["reason"])
        self.assertEqual(d2r.load(False), [])  # nothing written yet
        self.assertEqual(self.notified(), [])
        self.assertIsNone(self.hook("Stop", last_assistant_message=line(), stop_hook_active=True))
        r, = self.active()
        self.assertEqual((r["title"], r.get("format"), r["evidence"]["quote"]), (RULE, None, CORRECTION))
        self.assertIsNotNone(self.prompt("撤"))  # 撤 still takes back what this turn wrote

    def test_no_why_is_sent_back(self):
        """1121 鱼：每一个原则都要有其设定的原因和适用场景——两段（没有为什么）的不收，退回去补。"""
        out = self.turn(CORRECTION, f"好的。\n📌 记下了：{RULE}｜适用：{WHERE}。不要就说「撤」")
        self.assertEqual(out["decision"], "block")
        self.assertIn("少了第三段「为什么」", out["reason"])
        self.assertEqual(d2r.load(False), [])
        self.assertIsNone(self.hook("Stop", last_assistant_message=line(), stop_hook_active=True))
        r, = self.active()
        self.assertEqual(r["why"], WHY)

    def test_too_long_is_sent_back_once_then_kept_cut_and_marked(self):
        long_line = "📌 记下了：给用户的任何文件路径都要单独一行而且不跟标点｜适用：给路径时｜为什么：要能点开。不要就说「撤」"
        out = self.turn(CORRECTION, "好。\n" + long_line)
        self.assertIn("规矩 22 字，超过 16", out["reason"])
        self.assertIsNone(self.hook("Stop", last_assistant_message="好，我再想想。", stop_hook_active=True))
        r, = self.active()  # the fix never came: written anyway, cut to size, the whole line kept
        self.assertEqual(r["format"], "格式不全")
        self.assertEqual(len(r["title"]), live.TITLE_MAX)
        self.assertTrue(r["title"].endswith("…"))
        self.assertEqual(r["raw"], "给用户的任何文件路径都要单独一行而且不跟标点｜适用：给路径时｜为什么：要能点开")
        self.assertIn(f"[r001] {r['raw']} ｜证据", self.md())  # the model reads the whole line
        self.assertIn("格式不全", d2r.read_text(os.path.join(self.home, ".d2r", "规矩本.html")))

    def test_never_sent_back_twice(self):
        bad = "好。\n📌 记下了：当甲就乙。不要就说「撤」"
        self.assertEqual(self.turn("以后这样", bad)["decision"], "block")
        self.assertIsNone(self.hook("Stop", last_assistant_message=bad, stop_hook_active=True))
        self.assertEqual(self.active()[0]["format"], "格式不全")
        # a Stop that is already a continuation (another hook blocked) is never blocked by us
        self.assertIsNone(self.hook("Stop", last_assistant_message="📌 记下了：当丙就丁。不要就说「撤」",
                                    stop_hook_active=True))
        self.assertEqual(len(self.active()), 2)


class RetractTest(LiveBase):
    def test_che_right_after_the_line_deprecates_it(self):
        self.turn(CORRECTION, REPLY)
        ctx = self.prompt("撤")
        self.assertIn("[r001]", ctx)
        self.assertIn("作废", ctx)
        r, = d2r.load(False)
        self.assertEqual((r["status"], r["reason"]), ("deprecated", "用户说撤"))
        self.assertNotIn("[r001]", self.md())  # gone from CLAUDE.md, kept in the store
        self.assertIsNone(self.prompt("撤"))   # nothing left that the last reply announced

    def test_other_ways_of_saying_it(self):
        for i, word in enumerate(("不要这条", "撤。", "这条不要了", "别记")):
            self.turn(f"纠正{i}", "好。\n" + line(f"做事{i}", f"情形{i}"))
            self.assertIsNotNone(self.prompt(word), word)
        self.assertEqual(self.active(), [])

    def test_che_only_works_on_what_the_last_reply_announced(self):
        self.turn(CORRECTION, REPLY)
        self.turn("帮我把那篇稿子发出去", "发好了。")
        self.assertIsNone(self.prompt("撤"))       # means "unpublish", not "forget the rule"
        self.assertEqual(len(self.active()), 1)
        self.assertIsNone(self.prompt("撤", session="other-window"))
        self.assertEqual(len(self.active()), 1)

    def test_a_longer_sentence_with_che_is_left_to_the_model(self):
        self.turn(CORRECTION, REPLY)
        self.assertIsNone(self.prompt("把刚发的那条朋友圈撤了"))
        self.assertEqual(len(self.active()), 1)

    def test_undo_brings_it_back(self):
        self.turn(CORRECTION, REPLY)
        self.prompt("撤")
        subprocess.run([sys.executable, D2R, "undo"], capture_output=True, text=True, env=os.environ.copy())
        self.assertEqual(len(self.active()), 1)

    def test_cli_retract_takes_the_newest(self):
        self.turn("a", line("先甲", "做乙时"))
        self.turn("b", line("先丙", "做丁时", everywhere=True))
        out = subprocess.run([sys.executable, D2R, "retract"], capture_output=True, text=True, env=os.environ.copy())
        self.assertIn("已撤 g001", out.stdout)
        self.assertEqual([r["id"] for r in self.active()], ["r001"])
        subprocess.run([sys.executable, D2R, "retract"], capture_output=True, text=True, env=os.environ.copy())
        out = subprocess.run([sys.executable, D2R, "retract"], capture_output=True, text=True, env=os.environ.copy())
        self.assertNotEqual(out.returncode, 0)


class NotifyTest(LiveBase):
    def test_a_written_rule_shows_the_rule_first_then_where_it_applies(self):
        self.turn(CORRECTION, REPLY)
        b, = self.notified()
        self.assertEqual((b["title"], b["subtitle"], b["buttons"], b["rule"]),
                         ("规矩+1：" + RULE, "适用：" + WHERE, True, "r001"))
        self.assertEqual(b["body"], "不要就点「撤」，点「看看」看规矩本")
        self.assertNotIn("不适用", json.dumps(b, ensure_ascii=False))

    def test_clicking_opens_the_book_at_that_rule(self):
        self.turn(CORRECTION, REPLY)
        jump = self.notified()[0]["open"]
        r, = self.active()
        a = d2r.anchor(r, self.proj)
        self.assertEqual(jump, os.path.join(self.home, ".d2r", "规矩本_跳转", a + ".html"))
        self.assertIn("../规矩本.html#" + a, d2r.read_text(jump))
        book = d2r.read_text(os.path.join(self.home, ".d2r", "规矩本.html"))
        self.assertIn(f'id="{a}"', book)

    def test_che_shows_rule_minus_one(self):
        self.turn(CORRECTION, REPLY)
        self.prompt("撤")
        b = self.notified()[-1]
        self.assertEqual((b["title"], b["body"], b["buttons"]), ("规矩-1：" + RULE, "已撤，以后不再照做", False))

    def test_nothing_written_nothing_shown(self):
        self.turn("帮我看一眼", "看了，没问题")
        self.turn(CORRECTION, REPLY)
        self.turn(CORRECTION, REPLY)  # already a rule: written once, shown once
        self.prompt("撤")
        self.prompt("撤")  # nothing left to take back
        self.assertEqual([b["title"][:5] for b in self.notified()], ["规矩+1：", "规矩-1："])

    def test_notify_off(self):
        self.turn(CORRECTION, REPLY, env={"D2R_NOTIFY": "0"})
        self.assertEqual(self.notified(), [])
        self.assertEqual(len(self.active()), 1)


class ButtonTest(LiveBase):
    """What the background waiter does with alerter's answer."""
    def spec(self):
        self.turn(CORRECTION, REPLY)
        return self.notified()[0]

    def test_che_button_takes_back_that_very_rule(self):
        spec = self.spec()
        self.assertEqual(banner.act("撤", spec), "撤")
        r, = d2r.load(False)
        self.assertEqual((r["status"], r["reason"]), ("deprecated", "用户点了通知上的「撤」"))
        self.assertEqual(self.notified()[-1]["title"], "规矩-1：" + RULE)
        self.assertNotIn("[r001]", self.md())

    def test_kankan_or_a_click_on_the_banner_opens_the_book(self):
        spec = self.spec()
        for answer in ("看看", "@CONTENTCLICKED"):
            self.assertEqual(banner.act(answer, spec), "看看")
            self.assertEqual(self.notified()[-1], {"opened": spec["open"]})
        self.assertEqual(len(self.active()), 1)

    def test_no_click_leaves_the_rule_alone(self):
        spec = self.spec()
        for answer in ("@TIMEOUT", "@CLOSED", "好的", ""):
            self.assertIsNone(banner.act(answer, spec))
        self.assertEqual(len(self.active()), 1)
        self.assertEqual(len(self.notified()), 1)

    def test_alerter_args_follow_its_own_help(self):
        for helptext, d, close in (("-actions VALUE -closeLabel VALUE", "-", "-closeLabel"),
                                   ("--actions <actions> --close-label <close-label>", "--", "--close-label")):
            exe = os.path.join(self.tmp.name, "alerter")
            with open(exe, "w") as f:
                f.write(f"#!/bin/sh\necho '{helptext}'\n")
            os.chmod(exe, 0o755)
            args = banner.alerter_args(exe, self.spec() if d == "-" else self.notified()[0])
            self.assertIn(d + "actions", args)
            self.assertEqual(args[args.index(d + "actions") + 1], "撤,看看")
            self.assertEqual(args[args.index(close) + 1], "好的")
            self.assertEqual(args[args.index(d + "title") + 1], "规矩+1：" + RULE)
            self.assertEqual(args[args.index(d + "subtitle") + 1], "适用：" + WHERE)


class BookTest(LiveBase):
    def test_book_has_every_rule_newest_first_with_what_to_say(self):
        self.turn(CORRECTION, REPLY)
        self.turn("以后所有窗派活都先写来头", "好\n" + line("派活先写清为谁做", "派任何活时", everywhere=True))
        self.prompt("撤")
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        self.turn("又忘了，以后这个仓库提交前先跑测试", "好\n" + line("提交前先跑测试", "这个仓库每次提交"),
                  session="s2", env={"D2R_PROJECT": other}, cwd=other)
        book = d2r.read_text(os.path.join(self.home, ".d2r", "规矩本.html"))
        order = [book.index(x) for x in ("提交前先跑测试", "派活先写清为谁做", RULE)]
        self.assertEqual(order, sorted(order))  # newest on top, across projects
        self.assertIn("「" + CORRECTION + "」", book)
        for col in ("<dt>规矩</dt>", "<dt>适用</dt>", "<dt>证据原话</dt>"):
            self.assertIn(col, book)
        self.assertNotIn("不适用", book)
        self.assertIn("AI 被你纠正一次就记下，下次照做；不要就撤。", book)
        self.assertIn("已撤", book)
        self.assertIn("所有项目", book)
        self.assertIn("项目 proj", book)
        self.assertIn("项目 other", book)
        self.assertIn("在 proj 的窗里说「把 r001 撤了」", book)
        self.assertIn("生效 2 条，已撤 1 条", book)

    def test_old_rules_are_shown_as_they_were(self):
        sys.stdin, old = io.StringIO(json.dumps({"trigger": "给路径", "action": "单独一行", "scope": "project",
                                                 "evidence": {"quote": "原话"}})), sys.stdin
        try:
            with redirect_stdout(io.StringIO()):
                d2r.main(["add"])
        finally:
            sys.stdin = old
        book = d2r.read_text(os.path.join(self.home, ".d2r", "规矩本.html"))
        self.assertIn("当给路径就单独一行", book)
        self.assertIn("证据：「原话」", book)

    def test_text_is_escaped(self):
        self.turn("以后别再这样", "好\n" + line("<i>别用</i>斜体", "<script>写网页时"))
        book = d2r.read_text(os.path.join(self.home, ".d2r", "规矩本.html"))
        self.assertNotIn("<i>别用", book)
        self.assertNotIn("<script>写", book)
        self.assertIn("&lt;i&gt;别用&lt;/i&gt;斜体", book)

    def test_saying_guijuben_gets_the_path(self):
        self.turn(CORRECTION, REPLY)
        for ask in ("规矩本", "规矩本在哪", "打开规矩本"):
            ctx = self.prompt(ask)
            self.assertIn(os.path.join(self.home, ".d2r", "规矩本.html") + "\n" + os.path.join(self.home, ".d2r"), ctx)
        self.assertIsNotNone(self.prompt("撤"))  # asking for the book did not use up the 撤
        self.assertIsNone(self.prompt("帮我把规矩本这个词翻译成英文"))

    def test_cli_deprecate_rebuilds_the_book(self):
        self.turn(CORRECTION, REPLY)
        subprocess.run([sys.executable, D2R, "deprecate", "r001"], capture_output=True, text=True, env=os.environ.copy())
        self.assertIn("生效 0 条，已撤 1 条", d2r.read_text(os.path.join(self.home, ".d2r", "规矩本.html")))


class NotifierTest(unittest.TestCase):
    def test_fallback_says_how_to_see_everything(self):
        self.assertEqual(banner.BOOK_HINT, "\n看全部：说「规矩本」")
        self.assertTrue(banner.body_for(None, "plain").endswith(banner.BOOK_HINT))
        self.assertNotIn("点「撤」", banner.body_for(None, "click"))  # no button, no 点「撤」

    def test_install_never_reaches_outside_the_plugin(self):
        self.assertTrue(banner.VENDOR.startswith(ROOT + os.sep))
        self.assertEqual({t["repo"] for t in banner.TOOLS.values()}, {"vjeantet/alerter", "julienXX/terminal-notifier"})


@unittest.skipUnless(sys.platform == "darwin", "ditto is macOS only")
class InstallTest(unittest.TestCase):
    """The download path, with a local zip standing in for the GitHub release."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = os.path.join(self.tmp.name, "src", "alerter_v1")
        os.makedirs(src)
        with open(os.path.join(src, "alerter"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        self.zip = os.path.join(self.tmp.name, "alerter.zip")
        subprocess.run(["ditto", "-c", "-k", os.path.join(self.tmp.name, "src"), self.zip], check=True)
        with open(self.zip, "rb") as f:
            self.digest = hashlib.sha256(f.read()).hexdigest()
        self.saved = (banner.VENDOR, banner.STATE)
        banner.VENDOR = os.path.join(self.tmp.name, "vendor")
        banner.STATE = os.path.join(banner.VENDOR, "install.json")
        self.path = os.environ.get("PATH")
        os.environ["PATH"] = "/usr/bin:/bin"  # a real alerter on PATH must not count

    def tearDown(self):
        banner.VENDOR, banner.STATE = self.saved
        os.environ["PATH"] = self.path
        self.tmp.cleanup()

    def release(self, digest):
        return {"assets": [{"name": "alerter_v1_darwin_amd64.zip", "browser_download_url": "https://example/a.zip",
                            "digest": digest}]}

    def download(self, url, dest):
        shutil.copy(self.zip, dest)

    def test_installs_into_vendor_checked_against_githubs_digest(self):
        st = banner.install_one("alerter", self.release("sha256:" + self.digest), self.download)
        self.assertEqual((st["ok"], st["sha256"], st["checked"]), (True, self.digest, True), st)
        self.assertEqual(banner.tool_path("alerter"), os.path.join(banner.VENDOR, "alerter"))
        self.assertFalse(os.path.exists(os.path.join(banner.VENDOR, ".download-alerter")))

    def test_wrong_digest_installs_nothing(self):
        st = banner.install_one("alerter", self.release("sha256:" + "0" * 64), self.download)
        self.assertFalse(st["ok"])
        self.assertIn("校验对不上", st["err"])
        self.assertIsNone(banner.tool_path("alerter"))


class SessionStartTest(LiveBase):
    def add(self, n, scope, status="active"):
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"trigger": f"情形{n}", "action": f"做{n}", "scope": scope,
                                            "evidence": {"quote": f"原话{n}"}}))
        try:
            with redirect_stdout(io.StringIO()):
                d2r.main(["add"])
        finally:
            sys.stdin = old

    def test_the_convention_is_told_once_per_window_with_only_one_word_to_know(self):
        text = self.start()
        self.assertIn("📌 记下了：<规矩一句话>｜适用：<什么时候照做>｜为什么：<防什么问题、为谁>。不要就说「撤」", text)
        self.assertNotIn("不适用", text)
        self.assertIn("（所有项目）", text)
        for word in ("回 y", "／ p", "g 改", "promote", "合并"):
            self.assertNotIn(word, text)
        self.assertEqual(live.marks(text), [])  # the instruction itself is never mistaken for a rule
        self.assertIn("📌 记下了", self.start("compact"))  # a compact forgets it, so it comes back

    def test_active_task_rules_come_back_in_a_new_window(self):
        self.add(1, "task")
        self.add(2, "task")
        self.add(3, "project")  # already lives in CLAUDE.md, not repeated here
        d2r.main(["deprecate", "r001"])
        text = self.start()
        self.assertIn("[r002] 当情形2 → 做 做2", text)
        self.assertNotIn("[r001]", text)
        self.assertNotIn("[r003]", text)
        self.assertIn("[r002]", self.start("compact"))

    def test_many_task_rules_are_capped(self):
        for i in range(1, live.TASK_RULES_SHOWN + 4):
            self.add(i, "task")
        text = self.start()
        self.assertEqual(text.count("- [r"), live.TASK_RULES_SHOWN)
        self.assertIn("另有 3 条", text)

    def test_old_queue_goes_live_and_is_told_once_then_che_works(self):
        d2r.save_cands([
            {"id": "c001", "trigger": "做完一件活回报时", "action": "单列一段「我默认了什么」", "prohibit": "",
             "scope": "global", "evidence": {"quote": "让Agent在任务完成时说明自己的默认条件"}, "date": "2026-11-14"},
            {"id": "c002", "trigger": "t", "action": "a", "scope": "task", "evidence": {"quote": "q"}}])
        text = self.start(session="s9")
        self.assertIn("📌 之前还记下了：当做完一件活回报时就单列一段「我默认了什么」（所有项目）；当t就a。不要就说「撤」", text)
        self.assertEqual(d2r.load_cands(), [])
        g, = self.active(True)
        self.assertEqual((g["id"], g["evidence"]["date"], g["source"]), ("g001", "2026-11-14", "candidate"))
        self.assertEqual(self.active()[0]["scope"], "project")  # task is no default any more
        self.assertNotIn("之前还记下了", self.start(session="s9"))
        self.assertNotIn("之前还记下了", self.start("compact", session="s9"))
        # the first reply carries the notice line; 撤 then takes back the newest of them
        self.stop("好的。\n\n📌 之前还记下了：当做完一件活回报时就单列一段「我默认了什么」；当t就a。不要就说「撤」",
                  session="s9")
        self.assertIn("[r001]", self.prompt("撤", session="s9"))
        self.assertEqual([r["id"] for r in self.active()], [])
        self.assertEqual(len(self.active(True)), 1)

    def test_retracted_before_shown_is_not_told(self):
        d2r.save_cands([{"id": "c001", "trigger": "t", "action": "a", "scope": "project", "evidence": {"quote": "q"}}])
        d2r.migrate_cands("claude")
        d2r.main(["deprecate", "r001"])
        self.assertNotIn("之前还记下了", self.start())


if __name__ == "__main__":
    unittest.main()
