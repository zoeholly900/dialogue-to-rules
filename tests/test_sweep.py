"""D2R_MODE=sweep: fake transcripts only. Nothing here reads a real session or calls a real model.
Since 0.3 what the sweep picks goes live at once (no y/n queue); live.py tells the user next window."""
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
LIVE = os.path.join(ROOT, "hooks", "live.py")

QUOTE_A = "以后给我文件路径，文件一行、它所在的文件夹再一行，我说过很多遍了"
QUOTE_B = "每次派活先说清楚是给谁做的"

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
rec = {"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read(),
       "child": os.environ.get("D2R_SWEEP_CHILD")}
merge = os.environ.get("FAKE_MERGE_REPLY")
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\\n")
if os.environ.get("FAKE_FAIL"):
    sys.exit(3)
if merge and "规则卡主编" in rec["stdin"]:
    sys.stdout.write(open(merge, encoding="utf-8").read())
else:
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
        self.write_config({"repeat_times": 1, "min_score": 0})   # the flow tests are not about strictness
        self.env = {k: os.environ.get(k) for k in
                    ("D2R_PROJECT", "D2R_HOME", "D2R_HOST", "D2R_MODE", "D2R_OFF", "D2R_CLAUDE_BIN",
                     "D2R_SWEEP_SYNC", "D2R_SWEEP_CHILD", "FAKE_LOG", "FAKE_REPLY", "FAKE_FAIL", "FAKE_MERGE_REPLY")}
        for k in self.env:
            os.environ.pop(k, None)
        os.environ.update(D2R_PROJECT=self.proj, D2R_HOME=self.home, D2R_HOST="claude", D2R_MODE="sweep",
                          D2R_CLAUDE_BIN=self.fake, FAKE_LOG=self.fake_log, FAKE_REPLY=self.reply)

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def write_config(self, cfg, where=None):
        path = os.path.join(where or self.proj, ".d2r", "config.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

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
        """What the sweep wrote, oldest first. It goes live at once, so these are rules (ids r…/g…)."""
        out = []
        for e in d2r.load_jsonl(d2r.recent_path()):
            r = d2r.find_rule(e["id"])[2]
            if r and r.get("source") == "sweep" and r not in out:
                out.append(r)
        return out

    def live_start(self, source="startup"):
        payload = {"hook_event_name": "SessionStart", "session_id": "sess-0002", "cwd": self.proj,
                   "source": source}
        r = subprocess.run([sys.executable, LIVE], input=json.dumps(payload), capture_output=True,
                           text=True, env=os.environ.copy(), timeout=30)
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

    def log_text(self):
        return d2r.read_text(os.path.join(self.proj, ".d2r", "sweep.log")) or ""


class QuietModeTest(unittest.TestCase):
    def run_detect(self, prompt, mode):
        env = {k: v for k, v in {**os.environ, "D2R_MODE": mode}.items() if v != ""}
        return subprocess.run([sys.executable, os.path.join(ROOT, "hooks", "detect.py")],
                              input=json.dumps({"prompt": prompt}), capture_output=True, text=True, env=env)

    def test_live_is_the_default_and_detect_injects_nothing_even_on_a_regex_hit(self):
        for prompt in ("别再给我加 emoji 了", "帮我看一眼这个文件"):
            r = self.run_detect(prompt, "")
            self.assertEqual((r.returncode, r.stdout), (0, ""))
            self.assertEqual(self.run_detect(prompt, "live").stdout, "")
            self.assertEqual(self.run_detect(prompt, "sweep").stdout, "")

    def test_old_modes_still_inject(self):
        self.assertIn("📌 记下了", self.run_detect("帮我看一眼这个文件", "nudge").stdout)
        self.assertIn("📌 记下了", self.run_detect("别再给我加 emoji 了", "regex").stdout)
        self.assertEqual(self.run_detect("帮我看一眼这个文件", "regex").stdout, "")

    def test_hooks_json_wires_the_events(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as f:
            hooks = json.load(f)["hooks"]
        def cmds(ev):
            return " ".join(h["command"] for g in hooks[ev] for h in g["hooks"])
        for ev in ("SessionStart", "Stop", "UserPromptSubmit"):
            self.assertIn("hooks/live.py", cmds(ev))
        for ev in ("SessionEnd", "PreCompact"):
            self.assertIn("hooks/sweep.py", cmds(ev))
        self.assertIn("hooks/detect.py", cmds("UserPromptSubmit"))  # old modes can be switched back to


class TranscriptTest(SweepBase):
    def test_only_user_talk_with_a_short_assistant_tail(self):
        msgs = sweep.read_transcript(self.transcript)
        texts = [m[2] for m in msgs]
        self.assertEqual(len(msgs), 3)
        self.assertEqual(texts[0], "帮我把周报整理一下")
        joined = "\n".join(texts)
        for secret in ("TOOL-RESULT", "META-SECRET", "SIDECHAIN", "REMINDER", "SUMMARY", "command-name"):
            self.assertNotIn(secret, joined)
        chunks = sweep.build_chunks(msgs)
        self.assertEqual(len(chunks), 1)
        self.assertIn("[助手上一条的片段]", chunks[0])
        self.assertLess(len(chunks[0]), 1500)

    def test_long_assistant_tail_is_cut_to_head_and_end(self):
        t = sweep.tail("头" * 400 + "尾" * 400)
        self.assertLessEqual(len(t), sweep.TAIL_CAP + 5)
        self.assertTrue(t.startswith("头") and t.endswith("尾") and "…" in t)

    def test_over_budget_is_cut_into_chunks_and_nothing_is_dropped(self):
        old, sweep.CHUNK_CHARS = sweep.CHUNK_CHARS, 200
        try:
            msgs = [(i, "user", f"第{i}条" + "字" * 100, "") for i in range(5)]
            chunks = sweep.build_chunks(msgs)
        finally:
            sweep.CHUNK_CHARS = old
        self.assertGreater(len(chunks), 1)
        joined = "\n".join(chunks)
        for i in range(5):
            self.assertIn(f"第{i}条", joined)
        self.assertTrue(chunks[0].startswith("[用户] 第0条"))

    def test_card_cap_grows_with_session_length(self):
        cfg = {"max_per_day": 10}
        self.assertEqual(sweep.max_candidates(5, cfg), sweep.MAX_CANDIDATES)
        self.assertGreater(sweep.max_candidates(500, cfg), sweep.max_candidates(100, cfg))
        self.assertEqual(sweep.max_candidates(5000, cfg), 10)
        self.assertEqual(sweep.max_candidates(5000, {"max_per_day": 25}), 25)
        self.assertEqual(sweep.max_candidates(5, {"max_per_day": 2}), 2)


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
        self.assertEqual(out[0]["scope"], "project")  # no scope given: project, not task

    def test_cap_and_dedupe(self):
        texts = ["甲乙丙丁戊己庚"]
        cands = [{"quote": q, "trigger": "t", "action": "a"} for q in "甲乙丙丁戊"]
        self.assertEqual(len(sweep.validate(cands, texts, set())), sweep.MAX_CANDIDATES)
        self.assertEqual(len(sweep.validate(cands, texts, set(), cap=5)), 5)
        self.assertEqual(sweep.validate(cands[:1], texts, {"甲"}), [])

    def test_parse_tolerates_wrapping_and_rejects_junk(self):
        self.assertEqual(sweep.parse_candidates('好的：\n```json\n{"candidates": []}\n```'), [])
        for bad in ("没有 json", '{"candidates": "x"}', "{坏的}"):
            with self.assertRaises(RuntimeError):
                sweep.parse_candidates(bad)


class FullFlowTest(SweepBase):
    def test_end_to_end_sweep_goes_live_and_is_told_next_window(self):
        # 1. session ends -> sweep -> rules written at once (the invented quote is dropped)
        r = self.hook("SessionEnd", {"reason": "other"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        cands = self.cands()
        self.assertEqual([c["id"] for c in cands], ["g001", "r001"])
        self.assertEqual(cands[0]["evidence"]["quote"], QUOTE_A)
        self.assertEqual(cands[1]["scope"], "project")  # unknown scope falls to project, not task
        self.assertIn(QUOTE_A, d2r.read_text(os.path.join(self.home, ".claude", "CLAUDE.md")))
        self.assertIn(QUOTE_B, d2r.read_text(os.path.join(self.proj, "CLAUDE.md")))
        self.assertEqual(d2r.load_cands(), [])  # nothing waits for a y/n
        # the judge was called the way the work order says, from a clean directory
        call, = self.fake_calls()
        self.assertEqual(call["argv"][:4], ["-p", "--model", "claude-sonnet-5-5", "--tools"])
        for flag in ("--setting-sources", "--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, call["argv"])
        self.assertNotEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.proj))
        self.assertEqual(call["child"], "1")
        self.assertIn(QUOTE_A, call["stdin"])
        for secret in ("TOOL-RESULT", "META-SECRET", "SIDECHAIN", "REMINDER", "SUMMARY"):
            self.assertNotIn(secret, call["stdin"])
        # 2. next window: the user is told once, in one line, with 撤 as the only option
        text = self.live_start()
        self.assertIn("📌 之前还记下了：当给用户文件路径就文件路径一行、所在文件夹再一行（所有项目）；当派活就先写清为谁做", text)
        self.assertIn("不要就说「撤」", text)
        for word in ("回 y", "promote", "／ p 改"):
            self.assertNotIn(word, text)
        self.assertNotIn("之前还记下了", self.live_start())  # told once

    def test_taken_back_quote_is_not_written_again(self):
        self.hook("SessionEnd")
        self.d2r("deprecate", "g001")
        write_transcript(self.transcript, transcript_lines())  # same talk, new session id
        self.hook("SessionEnd", {"session_id": "sess-0002"})
        self.assertEqual([c["id"] for c in self.cands()], ["g001", "r001"])

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
        self.assertNotIn(QUOTE_A, self.fake_calls()[1]["stdin"].split("<对话>")[-1])  # only the new talk is read
        self.assertEqual(self.cands()[-1]["evidence"]["quote"], "别在我睡觉的时候发消息")

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
        self.assertIn("记下 0 条", self.log_text())
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
        for env in ({"D2R_MODE": ""}, {"D2R_MODE": "live"}, {"D2R_MODE": "nudge"}, {"D2R_MODE": "regex"},
                    {"D2R_OFF": "1"}, {"D2R_SWEEP_CHILD": "1"}):
            self.assertEqual(self.hook("SessionEnd", env=env).stdout, "")
            self.assertEqual(self.cands(), [])
            self.assertEqual(self.fake_calls(), [])  # the sweep is opt-in: the default (live) never calls a model
        self.assertEqual(self.hook("SessionStart").stdout, "")  # SessionStart is live.py's job now

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


class LongSessionTest(SweepBase):
    """A day-long session: read in chunks, merged, ranked; nothing from the early part is lost."""

    def write_long(self, n=40):  # 12 KB chunks would be one chunk here, so setUp shrinks them
        lines = []
        for i in range(n):
            lines.append(user(f"第{i}条，说点别的事情" + "字" * 60))
            lines.append(assistant("好。"))
        lines.insert(3, user("早上说的：没用的入口直接删，不然误导其他伙计"))
        lines.append(user("傍晚说的：别让写的人自己检查自己，找别家来看"))
        write_transcript(self.transcript, lines)

    def setUp(self):
        super().setUp()
        self.old = sweep.CHUNK_CHARS
        sweep.CHUNK_CHARS = 800
        os.environ["D2R_CHUNK_CHARS"] = "800"  # the hook runs as a subprocess

    def tearDown(self):
        sweep.CHUNK_CHARS = self.old
        os.environ.pop("D2R_CHUNK_CHARS", None)
        super().tearDown()

    def card(self, quote, **kw):
        return {"quote": quote, "trigger": "t " + quote[:6], "action": "a", "scope": "global", **kw}

    def test_every_chunk_is_judged_then_merged_and_the_early_part_counts(self):
        self.write_long()
        self.set_reply({"candidates": [self.card("没用的入口直接删，不然误导其他伙计"),
                                       self.card("别让写的人自己检查自己，找别家来看")]})
        merge = os.path.join(self.tmp.name, "merge.json")
        with open(merge, "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": 1, "explicit": True, "dup_of": None, "scope": "project"},
                                 {"id": 2, "explicit": True, "dup_of": None, "scope": "global"}]}, f)
        os.environ["FAKE_MERGE_REPLY"] = merge
        self.hook("SessionEnd")
        calls = self.fake_calls()
        judges = [c for c in calls if "规则卡筛选员" in c["stdin"]]
        merges = [c for c in calls if "规则卡主编" in c["stdin"]]
        self.assertGreater(len(judges), 2)
        self.assertEqual(len(merges), 1)
        seen = "\n".join(c["stdin"] for c in judges)
        self.assertIn("早上说的", seen)
        self.assertIn("傍晚说的", seen)
        cands = self.cands()
        # chunk order, scope comes from the merge step
        self.assertEqual([c["evidence"]["quote"] for c in cands],
                         ["没用的入口直接删，不然误导其他伙计", "别让写的人自己检查自己，找别家来看"])
        self.assertEqual([c["scope"] for c in cands], ["project", "global"])
        self.assertIn("40 条用户消息".replace("40", str(len(sweep.read_transcript(self.transcript)))), self.log_text())

    def test_a_broken_merge_falls_back_to_chunk_order_and_cap(self):
        self.write_long()
        self.set_reply({"candidates": [self.card("没用的入口直接删，不然误导其他伙计")]})
        merge = os.path.join(self.tmp.name, "merge.json")
        with open(merge, "w", encoding="utf-8") as f:
            f.write("我不会输出 JSON")
        os.environ["FAKE_MERGE_REPLY"] = merge
        self.hook("SessionEnd")
        self.assertEqual([c["evidence"]["quote"] for c in self.cands()],
                         ["没用的入口直接删，不然误导其他伙计"])

    def test_one_failed_chunk_keeps_the_rest_and_rereads_next_time(self):
        self.write_long()
        self.set_reply({"candidates": [self.card("没用的入口直接删，不然误导其他伙计")]})
        real = sweep.call_judge
        n = {"i": 0}

        def flaky(prompt, model=None):
            n["i"] += 1
            if n["i"] == 2:
                raise RuntimeError("boom")
            return real(prompt, model)
        sweep.call_judge = flaky
        try:
            sweep.use_project(self.proj)
            sweep.sweep(self.transcript, "sess-0001-abcd")
        finally:
            sweep.call_judge = real
        self.assertEqual(len(self.cands()), 1)
        self.assertEqual(sweep.load_state().get("sessions", {}), {})  # not marked as done
        self.assertIn("没判成", self.log_text())


class MergeLabelsTest(SweepBase):
    def cards(self, n):
        return [{"trigger": f"t{i}", "action": "a", "prohibit": "", "scope": "task", "before": "",
                 "evidence": {"quote": f"原话{i}"}, "_msg": i - 1} for i in range(1, n + 1)]

    def merge(self, items, themes=(), n=5, texts=None, known="", cfg=None):
        self.set_reply({"items": items, "themes": list(themes)})
        return sweep.merge_candidates(self.cards(n), known, texts or [f"原话{i}" for i in range(1, n + 1)],
                                      cfg or sweep.settings())

    def picked(self, entries, cfg=None, cap=20):
        cfg = dict(cfg or sweep.settings(), repeat_times=1, min_score=0)
        return [c["evidence"]["quote"] for c in sweep.pick(entries, cfg, cap)]

    def test_drop_theme_known_and_skipped_cards(self):
        entries = self.merge([
            {"id": 1, "explicit": False},                          # a "no" anywhere drops the card
            {"id": 2, "explicit": True, "general": True, "concrete": True, "known": False, "scope": "global"},
            {"id": 4, "known": True, "known_as": "别让 **写的人** 自己评自己"},   # really in the user's memory
            # ids 3 and 5 skipped by the model: kept, not lost
        ], themes=[[3, 5]], known="- ⭐ **别让写的人自己评自己**（鱼 1114）→ `x`")
        self.assertEqual(sorted(self.picked(entries)), ["原话2", "原话3", "原话5"])   # 3 and 5 each stay themselves
        by = {c["evidence"]["quote"]: (c, m, t) for c, m, t in entries}
        self.assertEqual(by["原话2"][0]["scope"], "global")
        self.assertEqual(sorted(by["原话3"][1]), ["原话3", "原话5"])   # ...and each carries both voices
        self.assertEqual(by["原话3"][2], (3, 5))

    def test_a_known_card_takes_its_whole_theme_with_it(self):
        entries = self.merge([{"id": 2, "known": True, "known_as": "别让写的人自己评自己"}], themes=[[1, 2, 3]], n=3,
                             known="- 别让写的人自己评自己")
        self.assertEqual(self.picked(entries), [])

    def test_a_theme_still_counts_the_voices_the_model_ruled_out(self):
        entries = self.merge([{"id": 2, "general": False}], themes=[[1, 2]], n=2)
        (card, said, _), = entries
        self.assertEqual(card["evidence"]["quote"], "原话1")
        self.assertEqual(sorted(said), ["原话1", "原话2"])

    def test_an_oversized_theme_is_not_trusted(self):
        n = sweep.THEME_MAX + 1
        entries = self.merge([], themes=[list(range(1, n + 1))], n=n)
        self.assertEqual(len(entries), n)
        for _, said, theme in entries:
            self.assertEqual((len(said), theme), (1, None))   # no shared voices, nothing folded

    def test_at_most_two_cards_of_one_theme_are_shown(self):
        texts = ["平淡一", "平淡二", "平淡三", "平淡四", "平淡五"]
        entries = self.merge([], themes=[[1, 2, 3]], texts=texts)
        self.assertEqual(self.picked(entries), ["原话1", "原话2", "原话4", "原话5"])   # the third of the theme is held back

    def test_known_needs_a_citation_that_is_really_in_the_known_text(self):
        known = "- 别让写的人自己评自己"
        entries = self.merge([
            {"id": 1, "known": True},                                        # a feeling, no citation
            {"id": 2, "known": True, "known_as": "模型凭空编的一句话"},          # citation not in the text
            {"id": 3, "known": True, "known_as": "写的人自己评自己"},           # real
        ], n=3, known=known)
        self.assertEqual(sorted(self.picked(entries)), ["原话1", "原话2"])

    def test_order_comes_from_the_users_own_signals_not_the_model(self):
        texts = ["平淡的一句", "这个我说了一百遍了", "以后别这样", "平淡又一句", "平淡第三句"]
        entries = self.merge([], texts=texts)
        # 2 and 3 carry keywords (3 each) and keep chunk order; 1, 4, 5 have nothing
        self.assertEqual(self.picked(entries), ["原话2", "原话3", "原话1", "原话4", "原话5"])

    def test_saying_it_again_counts_even_in_plain_words(self):
        texts = ["平淡一", "平淡二", "平淡三", "平淡四", "平淡五"]
        entries = self.merge([], themes=[[2, 4, 5]], texts=texts)
        self.assertEqual(self.picked(entries), ["原话2", "原话4", "原话1", "原话3"])   # three voices outrank the rest; 5 is the third of its theme

    def test_identical_messages_are_one_voice(self):
        cfg = sweep.settings()
        self.assertEqual(sweep.strength(["一样的话", "一样的话"], cfg), sweep.strength(["一样的话"], cfg))
        self.assertGreater(sweep.strength(["甲", "乙"], cfg), sweep.strength(["甲"], cfg))

    def test_junk_labels_do_not_crash_and_all_dropped_falls_back(self):
        entries = self.merge([{"id": "x"}, "junk", {"id": 99}], themes=[[1, 1], "x", [2, 77], 5], n=3)
        self.assertEqual(self.picked(entries), ["原话1", "原话2", "原话3"])
        entries = self.merge([{"id": i, "concrete": False} for i in (1, 2, 3)], n=3)
        self.assertEqual(self.picked(entries), ["原话1", "原话2", "原话3"])   # all ruled out: fall back to every card


class ConfigTest(SweepBase):
    """.d2r/config.json: global ~/.d2r, project overrides it, nothing needed, junk ignored."""

    def entries(self, *voices_each):
        out = []
        for i, voices in enumerate(voices_each, 1):
            card = {"trigger": "t", "action": "a", "scope": "global", "evidence": {"quote": f"原话{i}"}}
            out.append((card, list(voices), None))
        return out

    def pick(self, entries, cap=10):
        return [c["evidence"]["quote"] for c in sweep.pick(entries, sweep.settings(), cap)]

    def test_no_file_means_the_documented_defaults(self):
        os.remove(os.path.join(self.proj, ".d2r", "config.json"))
        cfg = sweep.settings()
        for k, v in sweep.DEFAULTS.items():
            self.assertEqual(cfg[k], v)
        self.assertEqual((cfg["max_per_day"], cfg["min_score"], cfg["repeat_times"]), (10, 3, 2))
        self.assertEqual((cfg["chunk_model"], cfg["merge_model"]), ("claude-sonnet-5-5", "claude-opus-5-5"))
        self.assertEqual(cfg["known_files"], [])
        for w in ("求求", "说一百遍", "下次又", "以后", "所有窗", "通用", "别再"):
            self.assertIn(w, sweep.keywords_of(cfg))

    def test_project_overrides_global_key_by_key(self):
        self.write_config({"max_per_day": 4, "repeat_times": 3, "chunk_model": "claude-haiku-5-5"}, self.home)
        self.write_config({"repeat_times": 1})
        cfg = sweep.settings()
        self.assertEqual((cfg["max_per_day"], cfg["repeat_times"], cfg["chunk_model"]), (4, 1, "claude-haiku-5-5"))
        self.assertEqual(cfg["min_score"], 3)   # untouched keys keep the default

    def test_junk_values_and_broken_files_are_ignored_not_fatal(self):
        self.write_config({"max_per_day": "lots", "keywords": "求求", "min_score": -1, "merge_model": " "})
        cfg = sweep.settings()
        self.assertEqual((cfg["max_per_day"], cfg["keywords"], cfg["min_score"], cfg["merge_model"]),
                         (10, [], 3, "claude-opus-5-5"))
        with open(os.path.join(self.proj, ".d2r", "config.json"), "w", encoding="utf-8") as f:
            f.write("{坏的")
        self.assertEqual(sweep.settings()["max_per_day"], 10)
        self.assertIn("不是合法 JSON", self.log_text())

    def test_repeat_times_one_mention_needs_a_keyword(self):
        self.write_config({"repeat_times": 2, "min_score": 3})
        got = self.pick(self.entries(["你以后别这样"], ["平淡一句"], ["甲", "乙"], ["甲", "甲"]))
        # keyword alone passes; one plain voice does not; two distinct voices pass; the same message twice is one voice
        self.assertEqual(got, ["原话1", "原话3"])

    def test_own_keywords_and_points(self):
        self.write_config({"repeat_times": 2, "keywords": ["我的口头禅"], "keyword_points": 5})
        e = self.entries(["平淡"], ["这是我的口头禅"], ["以后"])
        self.assertEqual(self.pick(e), ["原话2", "原话3"])           # own keyword and a default one both count
        self.write_config({"use_default_keywords": False, "keywords": ["我的口头禅"], "repeat_times": 2})
        self.assertEqual(self.pick(e), ["原话2"])                    # "以后" no longer counts

    def test_min_score_and_max_per_day(self):
        self.write_config({"repeat_times": 1, "min_score": 6})
        e = self.entries(["以后别再", "说了一百遍"], ["以后"], ["平淡"])
        self.assertEqual(self.pick(e), ["原话1"])
        self.write_config({"repeat_times": 1, "min_score": 0, "max_per_day": 2})
        self.assertEqual(sweep.max_candidates(500, sweep.settings()), 2)
        self.assertEqual(len(self.pick(e, cap=2)), 2)

    def test_models_come_from_the_config(self):
        self.write_config({"chunk_model": "my-chunk-model", "merge_model": "my-merge-model"})
        write_transcript(self.transcript, [user(f"我说过很多遍了{i}" + "字" * 300) for i in range(6)])
        os.environ["D2R_CHUNK_CHARS"] = "400"
        self.set_reply({"candidates": [{"quote": "我说过很多遍了0", "trigger": "t", "action": "a"},
                                       {"quote": "我说过很多遍了1", "trigger": "t", "action": "a"}]})
        try:
            self.hook("SessionEnd")
        finally:
            os.environ.pop("D2R_CHUNK_CHARS")
        models = {("主编" if "规则卡主编" in c["stdin"] else "筛选"): c["argv"][c["argv"].index("--model") + 1]
                  for c in self.fake_calls()}
        self.assertEqual(models, {"筛选": "my-chunk-model", "主编": "my-merge-model"})

    def test_default_strictness_gates_a_real_sweep(self):
        os.remove(os.path.join(self.proj, ".d2r", "config.json"))   # factory settings
        self.hook("SessionEnd")
        # QUOTE_A has "以后" (a default keyword) and is queued; QUOTE_B ("每次派活…") is said once with no keyword
        self.assertEqual([c["evidence"]["quote"] for c in self.cands()], [QUOTE_A])
        self.set_reply({"candidates": [{"quote": "帮我把周报整理一下", "trigger": "t", "action": "a"}]})
        os.remove(os.path.join(self.proj, ".d2r", "sweep_state.json"))
        self.hook("SessionEnd")
        self.assertEqual([c["evidence"]["quote"] for c in self.cands()], [QUOTE_A])   # one plain mention, no keyword: not written

    def test_config_command_prints_what_is_in_effect(self):
        self.write_config({"max_per_day": 7})
        r = subprocess.run([sys.executable, SWEEP, "config"], capture_output=True, text=True,
                           env=os.environ.copy(), timeout=30)
        out = json.loads(r.stdout)
        self.assertEqual(out["effective"]["max_per_day"], 7)
        self.assertTrue(out["files"][os.path.join(self.proj, ".d2r", "config.json")])


class KnownMemoryTest(SweepBase):
    def test_off_by_default_and_reads_only_what_is_pointed_at(self):
        self.assertEqual(sweep.memory_known_block(), "")
        mem = os.path.join(self.tmp.name, "MEMORY.md")
        with open(mem, "w", encoding="utf-8") as f:
            f.write("- 记过的：路径不带句号")
        self.write_config({"known_files": [mem, os.path.join(self.tmp.name, "nope.md")]})
        self.assertIn("路径不带句号", sweep.memory_known_block())
        self.write_config({"known_files": ["rel.md"]})   # relative paths start from the project
        with open(os.path.join(self.proj, "rel.md"), "w", encoding="utf-8") as f:
            f.write("相对路径也行")
        self.assertIn("相对路径也行", sweep.memory_known_block())

    def test_memory_goes_to_the_merge_call_only(self):
        mem = os.path.join(self.tmp.name, "MEMORY.md")
        with open(mem, "w", encoding="utf-8") as f:
            f.write("MEMORY-INDEX-MARKER")
        self.write_config({"known_files": [mem], "repeat_times": 1, "min_score": 0})
        write_transcript(self.transcript, [user("甲" * 3000 + str(i)) for i in range(4)])
        os.environ["D2R_CHUNK_CHARS"] = "700"   # messages are shown at 600 chars: one chunk each
        self.set_reply({"candidates": [{"quote": "甲" * 50, "trigger": "t", "action": "a"},
                                       {"quote": "甲" * 60, "trigger": "t", "action": "a"}]})
        try:
            self.hook("SessionEnd")
        finally:
            os.environ.pop("D2R_CHUNK_CHARS")
        calls = self.fake_calls()
        self.assertTrue(any("规则卡筛选员" in c["stdin"] for c in calls))
        self.assertTrue(any("规则卡主编" in c["stdin"] for c in calls))
        for c in calls:
            self.assertEqual("MEMORY-INDEX-MARKER" in c["stdin"], "规则卡主编" in c["stdin"])


class CandidateCommandsTest(SweepBase):
    def test_unknown_ids_and_empty_queue(self):
        self.assertEqual(self.d2r("candidates").stdout, "")
        for argv in (("promote", "c009"), ("drop", "c009"), ("promote", "x1")):
            r = self.d2r(*argv)
            self.assertNotEqual(r.returncode, 0)
        self.assertEqual(d2r.load(False), [])

    def test_candidates_lists_the_old_queue(self):
        d2r.save_cands([{"id": "c001", "trigger": "t", "action": "a", "scope": "global",
                         "evidence": {"quote": QUOTE_A}}])
        out = self.d2r("candidates").stdout
        self.assertIn("c001", out)
        self.assertIn(QUOTE_A, out)


if __name__ == "__main__":
    unittest.main()
