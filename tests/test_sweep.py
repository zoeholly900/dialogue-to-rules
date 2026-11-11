"""D2R_MODE=sweep: fake transcripts only. Nothing here reads a real session or calls a real model."""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import d2r  # noqa: E402
import detect  # noqa: E402
import sweep  # noqa: E402

SWEEP = os.path.join(ROOT, "hooks", "sweep.py")
D2R = os.path.join(ROOT, "scripts", "d2r.py")

QUOTE_A = "以后给我文件路径，文件一行、它所在的文件夹再一行，我说过很多遍了"
QUOTE_B = "每次派活先说清楚是给谁做的"

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
rec = {"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read(),
       "child": os.environ.get("D2R_SWEEP_CHILD")}
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\\n")
if os.environ.get("FAKE_FAIL"):
    sys.exit(3)
sys.stdout.write(open(os.environ["FAKE_REPLY"], encoding="utf-8").read())
"""


def user(text, **kw):
    return {"type": "user", "message": {"role": "user", "content": text}, **kw}


def assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def transcript_lines():
    return [
        user("<command-name>/clear</command-name>"),
        user("帮我把周报整理一下"),
        assistant("好的，周报整理好了，放在 周报.md 里。" * 20 + "文件在桌面上。"),
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "TOOL-RESULT-SECRET"}]}},
        user("META-SECRET", isMeta=True),
        user("SIDECHAIN-SECRET", isSidechain=True),
        user("<system-reminder>REMINDER-SECRET</system-reminder>"),
        user("This session is being continued from a previous conversation SUMMARY-SECRET"),
        assistant("路径是 /Users/x/周报.md。"),
        user([{"type": "text", "text": QUOTE_A + "。顺便，标题再大一点"}]),
        assistant("好，改了。"),
        user("每次派活" + "先说清楚是给谁做的，别让我猜。"),
        "this line is not json {{{",
    ]


def write_transcript(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write((json.dumps(ln, ensure_ascii=False) if not isinstance(ln, str) else ln) + "\n")


class SweepBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.proj = os.path.join(t, "proj")
        self.home = os.path.join(t, "home")
        os.makedirs(self.proj)
        os.makedirs(self.home)
        self.transcript = os.path.join(t, "session.jsonl")
        write_transcript(self.transcript, transcript_lines())
        self.fake = os.path.join(t, "fake_claude")
        with open(self.fake, "w", encoding="utf-8") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IEXEC)
        self.reply = os.path.join(t, "reply.json")
        self.fake_log = os.path.join(t, "fake.log")
        self.set_reply({"candidates": [
            {"quote": QUOTE_A, "before": "给了一个裸路径", "trigger": "给用户文件路径",
             "action": "文件路径一行、所在文件夹再一行", "prohibit": "", "scope": "global",
             "test": "给我文件时，我应该两行路径"},
            {"quote": "用户其实没这么说过，这是模型编的", "trigger": "x", "action": "y"},
            {"quote": QUOTE_B, "before": "没写来头就派了", "trigger": "派活", "action": "先写清为谁做",
             "prohibit": "派说不清来头的活", "scope": "weird-scope", "test": "派活时先写来头"},
        ]})
        self.env = {k: os.environ.get(k) for k in
                    ("D2R_PROJECT", "D2R_HOME", "D2R_HOST", "D2R_MODE", "D2R_OFF", "D2R_CLAUDE_BIN",
                     "D2R_SWEEP_SYNC", "D2R_SWEEP_CHILD", "FAKE_LOG", "FAKE_REPLY", "FAKE_FAIL")}
        for k in self.env:
            os.environ.pop(k, None)
        os.environ.update(D2R_PROJECT=self.proj, D2R_HOME=self.home, D2R_HOST="claude",
                          D2R_CLAUDE_BIN=self.fake, FAKE_LOG=self.fake_log, FAKE_REPLY=self.reply)

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def set_reply(self, obj):
        with open(self.reply, "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))

    def fake_calls(self):
        if not os.path.exists(self.fake_log):
            return []
        with open(self.fake_log, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def hook(self, event, extra=None, env=None, sync=True):
        payload = {"hook_event_name": event, "session_id": "sess-0001-abcd", "cwd": self.proj,
                   "transcript_path": self.transcript, **(extra or {})}
        e = {**os.environ, **(env or {})}
        if sync:
            e["D2R_SWEEP_SYNC"] = "1"
        return subprocess.run([sys.executable, SWEEP, "hook"], input=json.dumps(payload),
                              capture_output=True, text=True, env=e, timeout=60)

    def d2r(self, *argv):
        return subprocess.run([sys.executable, D2R, *argv], capture_output=True, text=True,
                              env=os.environ.copy(), timeout=30)

    def cands(self):
        return d2r.load_cands()

    def log_text(self):
        return d2r.read_text(os.path.join(self.proj, ".d2r", "sweep.log")) or ""


class QuietModeTest(unittest.TestCase):
    def run_detect(self, prompt, mode):
        env = {k: v for k, v in {**os.environ, "D2R_MODE": mode}.items() if v != ""}
        return subprocess.run([sys.executable, os.path.join(ROOT, "hooks", "detect.py")],
                              input=json.dumps({"prompt": prompt}), capture_output=True, text=True, env=env)

    def test_sweep_is_the_default_and_injects_nothing_even_on_a_regex_hit(self):
        for prompt in ("别再给我加 emoji 了", "帮我看一眼这个文件"):
            r = self.run_detect(prompt, "")
            self.assertEqual((r.returncode, r.stdout), (0, ""))
            self.assertEqual(self.run_detect(prompt, "sweep").stdout, "")

    def test_old_modes_still_inject(self):
        self.assertIn("【规则卡", self.run_detect("帮我看一眼这个文件", "nudge").stdout)
        self.assertIn("【规则卡", self.run_detect("别再给我加 emoji 了", "regex").stdout)
        self.assertEqual(self.run_detect("帮我看一眼这个文件", "regex").stdout, "")

    def test_hooks_json_wires_the_three_events(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as f:
            hooks = json.load(f)["hooks"]
        for ev in ("SessionStart", "SessionEnd", "PreCompact"):
            self.assertIn("hooks/sweep.py", hooks[ev][0]["hooks"][0]["command"])
        self.assertIn("UserPromptSubmit", hooks)  # old modes can be switched back to


class TranscriptTest(SweepBase):
    def test_only_user_talk_with_a_short_assistant_tail(self):
        msgs = sweep.read_transcript(self.transcript)
        texts = [m[2] for m in msgs]
        self.assertEqual(len(msgs), 3)
        self.assertEqual(texts[0], "帮我把周报整理一下")
        joined = "\n".join(texts)
        for secret in ("TOOL-RESULT", "META-SECRET", "SIDECHAIN", "REMINDER", "SUMMARY", "command-name"):
            self.assertNotIn(secret, joined)
        convo = sweep.build_convo(msgs)
        self.assertIn("[助手上一条的片段]", convo)
        self.assertLess(len(convo), 1500)

    def test_long_assistant_tail_is_cut_to_head_and_end(self):
        t = sweep.tail("头" * 400 + "尾" * 400)
        self.assertLessEqual(len(t), sweep.TAIL_CAP + 5)
        self.assertTrue(t.startswith("头") and t.endswith("尾") and "…" in t)

    def test_over_budget_drops_the_oldest(self):
        old, sweep.BUDGET = sweep.BUDGET, 200
        try:
            msgs = [(i, "user", f"第{i}条" + "字" * 100, "") for i in range(5)]
            convo = sweep.build_convo(msgs)
        finally:
            sweep.BUDGET = old
        self.assertIn("第4条", convo)
        self.assertNotIn("第0条", convo)


class ValidateTest(unittest.TestCase):
    def test_evidence_must_be_a_verbatim_substring(self):
        texts = ["我说过很多遍了，别再绕", "另一条"]
        good = {"quote": "别再绕", "trigger": "t", "action": "a"}
        cases = [
            {**good, "quote": "别再绕弯"},                 # not in any message
            {**good, "quote": ""},
            {**good, "trigger": " "},
            {**good, "action": "", "prohibit": ""},
            {**good, "quote": "我说过很多遍了，" + "别再绕" + "，另一条"},  # stitched across messages
        ]
        self.assertEqual(sweep.validate(cases, texts, set()), [])
        out = sweep.validate([good], texts, set())
        self.assertEqual(out[0]["evidence"]["quote"], "别再绕")
        self.assertEqual(out[0]["scope"], "task")

    def test_cap_and_dedupe(self):
        texts = ["甲乙丙丁戊己庚"]
        cands = [{"quote": q, "trigger": "t", "action": "a"} for q in "甲乙丙丁戊"]
        self.assertEqual(len(sweep.validate(cands, texts, set())), sweep.MAX_CANDIDATES)
        self.assertEqual(sweep.validate(cands[:1], texts, {"甲"}), [])

    def test_parse_tolerates_wrapping_and_rejects_junk(self):
        self.assertEqual(sweep.parse_candidates('好的：\n```json\n{"candidates": []}\n```'), [])
        for bad in ("没有 json", '{"candidates": "x"}', "{坏的}"):
            with self.assertRaises(RuntimeError):
                sweep.parse_candidates(bad)


class FullFlowTest(SweepBase):
    def test_end_to_end_sweep_start_promote(self):
        # 1. session ends -> sweep -> candidates (the invented quote is dropped)
        r = self.hook("SessionEnd", {"reason": "other"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        cands = self.cands()
        self.assertEqual([c["id"] for c in cands], ["c001", "c002"])
        self.assertEqual(cands[0]["evidence"]["quote"], QUOTE_A)
        self.assertEqual(cands[0]["scope"], "global")
        self.assertEqual(cands[1]["scope"], "task")  # unknown scope falls back to task
        self.assertFalse(os.path.exists(os.path.join(self.proj, "CLAUDE.md")))  # nothing written as a rule
        self.assertEqual(d2r.load(False), [])
        # the judge was called the way the work order says, from a clean directory
        call, = self.fake_calls()
        self.assertEqual(call["argv"][:4], ["-p", "--model", "claude-haiku-5-5", "--tools"])
        for flag in ("--setting-sources", "--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, call["argv"])
        self.assertNotEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.proj))
        self.assertEqual(call["child"], "1")
        self.assertIn(QUOTE_A, call["stdin"])
        for secret in ("TOOL-RESULT", "META-SECRET", "SIDECHAIN", "REMINDER", "SUMMARY"):
            self.assertNotIn(secret, call["stdin"])
        # 2. next window in this project -> cards injected
        r = self.hook("SessionStart", {"source": "startup"})
        ctx = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        text = ctx["additionalContext"]
        self.assertIn("【规则卡·候选 c001·还没写入】", text)
        self.assertIn(f"证据：「{QUOTE_A}」", text)
        self.assertIn("回 y 才写入 ／ n 丢弃 ／ p 改 project ／ g 改 global", text)
        self.assertIn("promote <卡号>", text)
        self.assertEqual(text.count("【规则卡·候选"), 2)
        # 3. answer y on c001 (global) and n on c002
        out = self.d2r("promote", "c001")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("g001 已写入", out.stdout)
        self.assertIn(QUOTE_A, d2r.read_text(os.path.join(self.home, ".claude", "CLAUDE.md")))
        self.assertEqual([c["id"] for c in self.cands()], ["c002"])
        self.assertIn("c002 已丢弃", self.d2r("drop", "c002").stdout)
        self.assertEqual(self.cands(), [])
        self.assertFalse(os.path.exists(os.path.join(self.proj, ".d2r", "候选.jsonl")))
        # 4. nothing left -> nothing injected
        self.assertEqual(self.hook("SessionStart", {"source": "startup"}).stdout, "")

    def test_p_writes_project_scope_and_unanswered_cards_stay(self):
        self.hook("SessionEnd")
        self.assertIn("r001 已写入（范围 project）", self.d2r("promote", "c002", "--scope", "project").stdout)
        self.assertIn(QUOTE_B, d2r.read_text(os.path.join(self.proj, "CLAUDE.md")))
        for _ in range(2):  # asking again later just shows what is still there
            text = json.loads(self.hook("SessionStart").stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("候选 c001", text)
            self.assertNotIn("候选 c002", text)

    def test_dropped_quote_is_not_proposed_again(self):
        self.hook("SessionEnd")
        self.d2r("drop", "c001")
        write_transcript(self.transcript, transcript_lines())  # same talk, new session id
        self.hook("SessionEnd", {"session_id": "sess-0002"})
        self.assertNotIn(QUOTE_A, [c["evidence"]["quote"] for c in self.cands()])

    def test_precompact_then_sessionend_only_sweeps_new_lines(self):
        self.hook("PreCompact", {"trigger": "auto"})
        n = len(self.cands())
        self.assertEqual(len(self.fake_calls()), 1)
        self.hook("SessionEnd")  # nothing new since the compact -> no second model call
        self.assertEqual(len(self.fake_calls()), 1)
        self.assertEqual(len(self.cands()), n)
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(user("还有，以后别在我睡觉的时候发消息"), ensure_ascii=False) + "\n")
            f.write(json.dumps(user("听懂没"), ensure_ascii=False) + "\n")
        self.set_reply({"candidates": [{"quote": "别在我睡觉的时候发消息", "trigger": "晚上",
                                        "prohibit": "发消息"}]})
        self.hook("SessionEnd")
        self.assertEqual(len(self.fake_calls()), 2)
        self.assertNotIn(QUOTE_A, self.fake_calls()[1]["stdin"])
        self.assertEqual(self.cands()[-1]["id"], "c003")

    def test_candidate_ids_do_not_repeat_after_answers(self):
        self.hook("SessionEnd")
        self.d2r("drop", "c001")
        self.d2r("drop", "c002")
        write_transcript(self.transcript, transcript_lines()
                         + [user("再来一句，以后汇报先说结论、后说过程"), user("记住")])
        self.set_reply({"candidates": [{"quote": "汇报先说结论、后说过程", "trigger": "汇报", "action": "先说结论"}]})
        self.hook("SessionEnd")
        self.assertEqual([c["id"] for c in self.cands()], ["c003"])

    def test_failures_are_quiet_and_retry_next_time(self):
        r = self.hook("SessionEnd", env={"FAKE_FAIL": "1"})
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.cands(), [])
        self.assertIn("扫描失败", self.log_text())
        self.set_reply("我不会输出 JSON")
        self.hook("SessionEnd")
        self.assertEqual(self.cands(), [])
        self.assertEqual(self.log_text().count("扫描失败"), 2)
        self.set_reply({"candidates": []})  # the offset only moves on success: this retry still sees the talk
        self.hook("SessionEnd")
        self.assertEqual(len(self.fake_calls()), 3)
        self.assertIn("出 0 条候选", self.log_text())
        self.hook("SessionEnd")
        self.assertEqual(len(self.fake_calls()), 3)  # swept now, so no fourth call

    def test_missing_transcript_and_too_short_are_quiet(self):
        r = self.hook("SessionEnd", {"transcript_path": os.path.join(self.tmp.name, "nope.jsonl")})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertIn("读不了会话记录", self.log_text())
        write_transcript(self.transcript, [user("你好")])
        self.hook("SessionEnd")
        self.assertEqual(self.fake_calls(), [])
        self.assertIn("跳过", self.log_text())
        r = self.hook("SessionEnd", {"transcript_path": None})
        self.assertEqual(r.returncode, 0)

    def test_other_modes_off_and_recursion_guard_do_nothing(self):
        for env in ({"D2R_MODE": "nudge"}, {"D2R_MODE": "regex"}, {"D2R_OFF": "1"}, {"D2R_SWEEP_CHILD": "1"}):
            self.assertEqual(self.hook("SessionEnd", env=env).stdout, "")
            self.assertEqual(self.cands(), [])
            self.assertEqual(self.fake_calls(), [])
        d2r.save_cands([{"id": "c001", "trigger": "t", "action": "a", "scope": "task",
                         "evidence": {"quote": "q"}}])
        for env in ({"D2R_MODE": "nudge"}, {"D2R_OFF": "1"}):
            self.assertEqual(self.hook("SessionStart", env=env).stdout, "")

    def test_bad_stdin_is_silent(self):
        r = subprocess.run([sys.executable, SWEEP, "hook"], input="not json", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_detached_sweep_returns_at_once_and_finishes_in_background(self):
        t0 = time.time()
        r = self.hook("SessionEnd", sync=False)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertLess(time.time() - t0, 3)
        for _ in range(100):
            if self.cands():
                break
            time.sleep(0.1)
        self.assertEqual(len(self.cands()), 2)


class SessionStartTest(SweepBase):
    def start(self, source="startup"):
        r = self.hook("SessionStart", {"source": source})
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout else ""

    def add(self, n, scope, status="active"):
        buf = io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"trigger": f"情形{n}", "action": f"做{n}", "scope": scope,
                                            "evidence": {"quote": f"原话{n}"}}))
        try:
            with redirect_stdout(buf):
                d2r.main(["add"])
        finally:
            sys.stdin = old

    def test_nothing_to_say_means_no_output(self):
        self.assertEqual(self.start(), "")

    def test_active_task_rules_come_back_in_a_new_window(self):
        self.add(1, "task")
        self.add(2, "task")
        self.add(3, "project")  # already lives in CLAUDE.md, not repeated here
        d2r.main(["deprecate", "r001"])
        text = self.start()
        self.assertIn("[r002] 当情形2 → 做 做2", text)
        self.assertNotIn("[r001]", text)
        self.assertNotIn("[r003]", text)
        self.assertNotIn("候选", text)
        self.assertIn("[r002]", self.start("resume"))
        self.assertIn("[r002]", self.start("compact"))  # a compact forgets them, so they come back

    def test_compact_does_not_show_cards_again(self):
        self.add(1, "task")
        d2r.save_cands([{"id": "c001", "trigger": "t", "action": "a", "scope": "task",
                         "evidence": {"quote": "q"}}])
        self.assertIn("候选 c001", self.start("startup"))
        text = self.start("compact")
        self.assertNotIn("候选", text)
        self.assertIn("[r001]", text)

    def test_many_task_rules_are_capped(self):
        for i in range(1, sweep.TASK_RULES_SHOWN + 4):
            self.add(i, "task")
        text = self.start()
        self.assertEqual(text.count("- [r"), sweep.TASK_RULES_SHOWN)
        self.assertIn("另有 3 条", text)


class CandidateCommandsTest(SweepBase):
    def test_unknown_ids_and_empty_queue(self):
        self.assertEqual(self.d2r("candidates").stdout, "")
        for argv in (("promote", "c009"), ("drop", "c009"), ("promote", "x1")):
            r = self.d2r(*argv)
            self.assertNotEqual(r.returncode, 0)
        self.assertEqual(d2r.load(False), [])

    def test_candidates_lists_the_queue(self):
        self.hook("SessionEnd")
        out = self.d2r("candidates").stdout
        self.assertIn("c001", out)
        self.assertIn(QUOTE_A, out)


if __name__ == "__main__":
    unittest.main()
