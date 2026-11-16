"""D2R_MODE=live (default): the rule is written in the turn the user corrects, 撤 takes it back.
Hooks run as subprocesses with fake payloads; no real model is called."""
import hashlib
import io
import json
import os
import pathlib
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


class NotifyTest(LiveBase):
    def test_a_written_rule_shows_a_banner(self):
        self.turn(CORRECTION, REPLY)
        b, = self.notified()
        self.assertEqual({k: b[k] for k in ("title", "body", "subtitle")},
                         {"title": "记下一条规矩", "body": "当给用户文件路径就单独一行、后面不跟标点", "subtitle": "不要就回「撤」"})

    def test_clicking_opens_the_book_at_that_rule(self):
        self.turn(CORRECTION, REPLY)
        jump = self.notified()[0]["open"]
        r, = self.active()
        a = d2r.anchor(r, self.proj)
        self.assertEqual(jump, os.path.join(self.home, ".d2r", "规矩本_跳转", a + ".html"))
        self.assertIn("../规矩本.html#" + a, d2r.read_text(jump))
        book = d2r.read_text(os.path.join(self.home, ".d2r", "规矩本.html"))
        self.assertIn(f'id="{a}"', book)

    def test_all_projects_says_so_and_long_rules_are_cut(self):
        self.turn("以后所有窗都这样", "好\n📌 记下了（所有项目）：当派活，就先写清这活为谁做、解决谁的什么痛点、做完要达到什么目标，"
                  "窗名写清在干什么、谁要的、哪天派的，做完回报时单列一段自己默认了什么、哪里没实测。不要就说「撤」")
        b, = self.notified()
        self.assertEqual(b["title"], "记下一条规矩（所有项目）")
        self.assertEqual(len(b["body"]), live.NOTIFY_CHARS)
        self.assertTrue(b["body"].endswith("…"))

    def test_che_shows_a_banner_too(self):
        self.turn(CORRECTION, REPLY)
        self.prompt("撤")
        b = self.notified()[-1]
        self.assertEqual((b["title"], b["subtitle"]), ("已撤", ""))
        self.assertTrue(b["body"].startswith("已撤：当给用户文件路径"))

    def test_nothing_written_nothing_shown(self):
        self.turn("帮我看一眼", "看了，没问题")
        self.turn(CORRECTION, REPLY)
        self.turn(CORRECTION, REPLY)  # already a rule: written once, shown once
        self.prompt("撤")
        self.prompt("撤")  # nothing left to take back
        self.assertEqual([b["title"] for b in self.notified()], ["记下一条规矩", "已撤"])

    def test_notify_off(self):
        self.turn(CORRECTION, REPLY, env={"D2R_NOTIFY": "0"})
        self.assertEqual(self.notified(), [])
        self.assertEqual(len(self.active()), 1)


class BookTest(LiveBase):
    def test_book_has_every_rule_newest_first_with_what_to_say(self):
        self.turn(CORRECTION, REPLY)
        self.turn("以后所有窗派活都先写来头", "好\n📌 记下了（所有项目）：当派活就先写清为谁做。不要就说「撤」")
        self.prompt("撤")
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        self.turn("又忘了，以后这个仓库提交前先跑测试", "好\n📌 记下了：当提交前就先跑测试。不要就说「撤」",
                  session="s2", env={"D2R_PROJECT": other}, cwd=other)
        book = d2r.read_text(os.path.join(self.home, ".d2r", "规矩本.html"))
        order = [book.index(x) for x in ("当提交前就先跑测试", "当派活就先写清为谁做", "当给用户文件路径")]
        self.assertEqual(order, sorted(order))  # newest on top, across projects
        self.assertIn("证据：「" + CORRECTION + "」", book)
        self.assertIn("已撤", book)
        self.assertIn("所有项目", book)
        self.assertIn("项目 proj", book)
        self.assertIn("项目 other", book)
        self.assertIn("在 proj 的窗里说「把 r001 撤了」", book)
        self.assertIn("生效 2 条，已撤 1 条", book)

    def test_text_is_escaped(self):
        self.turn("以后别再这样", "好\n📌 记下了：当写 <script>alert(1)</script> 时就转义。不要就说「撤」")
        book = d2r.read_text(os.path.join(self.home, ".d2r", "规矩本.html"))
        self.assertNotIn("<script>alert", book)
        self.assertIn("&lt;script&gt;", book)

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
        self.assertEqual(live.BOOK_HINT, "\n看全部：说「规矩本」")

    def test_install_never_reaches_outside_the_plugin(self):
        for p in (live.VENDOR, live.TN_APP, live.TN_BIN, live.TN_STATE):
            self.assertTrue(p.startswith(ROOT + os.sep))
        self.assertTrue(live.TN_URL.startswith("https://github.com/julienXX/terminal-notifier/releases/download/"))


@unittest.skipUnless(sys.platform == "darwin", "ditto / terminal-notifier are macOS only")
class InstallTest(unittest.TestCase):
    """The download path with a local zip standing in for the GitHub release."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app = os.path.join(self.tmp.name, "src", "terminal-notifier-2.0.0", "terminal-notifier.app", "Contents", "MacOS")
        os.makedirs(app)
        with open(os.path.join(app, "terminal-notifier"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(app, "terminal-notifier"), 0o755)
        self.zip = os.path.join(self.tmp.name, "tn.zip")
        subprocess.run(["ditto", "-c", "-k", os.path.join(self.tmp.name, "src"), self.zip], check=True)
        vendor = os.path.join(self.tmp.name, "vendor")
        self.saved = {k: getattr(live, k) for k in ("VENDOR", "TN_APP", "TN_BIN", "TN_STATE", "TN_URL", "TN_SHA256")}
        live.VENDOR, live.TN_APP = vendor, os.path.join(vendor, "terminal-notifier.app")
        live.TN_BIN = os.path.join(live.TN_APP, "Contents", "MacOS", "terminal-notifier")
        live.TN_STATE, live.TN_URL = os.path.join(vendor, "install.json"), pathlib.Path(self.zip).as_uri()
        self.env = os.environ.get("D2R_PROJECT")
        os.environ["D2R_PROJECT"] = self.tmp.name

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(live, k, v)
        if self.env is None:
            os.environ.pop("D2R_PROJECT", None)
        else:
            os.environ["D2R_PROJECT"] = self.env
        self.tmp.cleanup()

    def test_installs_into_vendor_and_records_the_digest(self):
        with open(self.zip, "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
        live.TN_SHA256 = None
        live.install_notifier()
        st = live.read_install_state()
        self.assertEqual((st["ok"], st["sha256"]), (True, want))
        self.assertTrue(os.access(live.TN_BIN, os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(live.VENDOR, ".download")))

    def test_wrong_digest_installs_nothing(self):
        live.TN_SHA256 = "0" * 64
        live.install_notifier()
        self.assertFalse(live.read_install_state()["ok"])
        self.assertFalse(os.path.exists(live.TN_APP))


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
