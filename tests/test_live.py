"""D2R_MODE=live (default): the rule is written in the turn the user corrects, 撤 takes it back.
Hooks run as subprocesses with fake payloads; no real model is called."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import d2r  # noqa: E402
import live  # noqa: E402

LIVE = os.path.join(ROOT, "hooks", "live.py")
D2R = os.path.join(ROOT, "scripts", "d2r.py")

CORRECTION = "你又把路径后面带了句号，以后给我路径都单独一行，后面别跟标点"
REPLY = ("好的，改了：\n/Users/x/a.md\n/Users/x\n\n"
         "📌 记下了：当给用户文件路径就单独一行、后面不跟标点。不要就说「撤」")


class LiveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = os.path.join(self.tmp.name, "proj")
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.proj)
        os.makedirs(self.home)
        keys = ("D2R_PROJECT", "D2R_HOME", "D2R_HOST", "D2R_MODE", "D2R_OFF", "D2R_SWEEP_CHILD", "CLAUDE_PROJECT_DIR")
        self.env = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(D2R_PROJECT=self.proj, D2R_HOME=self.home, D2R_HOST="claude")

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


class ParseTest(unittest.TestCase):
    def test_the_line_and_its_variants(self):
        self.assertEqual(live.marks(REPLY), [("当给用户文件路径就单独一行、后面不跟标点", False)])
        self.assertEqual(live.marks("**📌 记下了（所有项目）**：当派活，就先写来头。不要的话就说「撤」"),
                         [("当派活，就先写来头", True)])
        self.assertEqual(live.marks("- 📌 记下了：当额度<30%就只收尾。不要就说'撤'"), [("当额度<30%就只收尾", False)])

    def test_templates_code_and_other_pins_are_not_rules(self):
        for text in ("📌 记下了：当<什么情形>就<怎么做>。不要就说「撤」",
                     "📌 记下了：当……就……。不要就说「撤」",
                     "```\n📌 记下了：当甲就乙。不要就说「撤」\n```",
                     "📌 之前还记下了：当甲就乙。不要就说「撤」",
                     "我会在末尾写 📌 记下了：当甲就乙"):  # not at the start of a line
            self.assertEqual(live.marks(text), [], text)

    def test_card_from_the_line(self):
        self.assertEqual(live.to_card("当派活，就先写来头"), {"trigger": "派活", "action": "先写来头", "prohibit": ""})
        self.assertEqual(live.to_card("当给路径就不要带句号"), {"trigger": "给路径", "action": "", "prohibit": "带句号"})
        self.assertEqual(live.to_card("汇报先说结论")["action"], "汇报先说结论")

    def test_long_message_evidence_is_the_closest_sentence_verbatim(self):
        msg = "今天先这样。" + "别的事情说了很多很多。" * 20 + "以后给我路径都单独一行，后面别跟标点。" + "收工吧。"
        q = live.best_quote(msg, live.to_card("当给用户路径就单独一行、后面不跟标点"))
        self.assertIn("路径都单独一行", q)
        self.assertIn(q, msg)


class CorrectionTurnTest(LiveBase):
    def test_the_correction_turn_writes_a_project_rule_with_the_users_words(self):
        self.assertIsNone(self.turn(CORRECTION, REPLY))
        r, = self.active()
        self.assertEqual((r["id"], r["scope"], r["source"]), ("r001", "project", "live"))
        self.assertEqual(r["trigger"], "给用户文件路径")
        self.assertEqual(r["action"], "单独一行、后面不跟标点")
        self.assertEqual(r["evidence"]["quote"], CORRECTION)
        self.assertIn("[r001] 当给用户文件路径", self.md())  # default-on: in CLAUDE.md with nobody answering
        self.assertEqual(self.md(True), "")

    def test_all_projects_goes_global(self):
        self.turn("所有窗都一样，派活先写来头", "好。\n📌 记下了（所有项目）：当派活就先写清为谁做。不要就说「撤」")
        self.assertEqual(self.active(), [])
        self.assertEqual(self.active(True)[0]["id"], "g001")
        self.assertIn("先写清为谁做", self.md(True))

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
        out = self.turn("以后都这样", "好。\n📌 记下了（所有项目）：当甲就乙。不要就说「撤」")
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
            self.turn(f"纠正{i}", f"好。\n📌 记下了：当情形{i}就做{i}。不要就说「撤」")
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
        self.turn("a", "📌 记下了：当甲就乙。不要就说「撤」")
        self.turn("b", "📌 记下了（所有项目）：当丙就丁。不要就说「撤」")
        out = subprocess.run([sys.executable, D2R, "retract"], capture_output=True, text=True, env=os.environ.copy())
        self.assertIn("已撤 g001", out.stdout)
        self.assertEqual([r["id"] for r in self.active()], ["r001"])
        subprocess.run([sys.executable, D2R, "retract"], capture_output=True, text=True, env=os.environ.copy())
        out = subprocess.run([sys.executable, D2R, "retract"], capture_output=True, text=True, env=os.environ.copy())
        self.assertNotEqual(out.returncode, 0)


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
        self.assertIn("📌 记下了：当<什么情形>就<怎么做>。不要就说「撤」", text)
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
