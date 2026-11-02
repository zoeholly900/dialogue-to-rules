import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import d2r  # noqa: E402


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def card(n, scope="project", **kw):
    c = {"trigger": f"情形{n}", "action": f"做{n}", "scope": scope,
         "evidence": {"quote": f"用户原话{n}"}}
    c.update(kw)
    return c


class D2RTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = os.path.join(self.tmp.name, "proj")
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.proj)
        os.makedirs(self.home)
        self.env = {k: os.environ.get(k) for k in ("D2R_PROJECT", "D2R_HOME", "D2R_HOST")}
        os.environ.update(D2R_PROJECT=self.proj, D2R_HOME=self.home, D2R_HOST="claude")

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    # helpers
    def cli(self, *argv, stdin=None):
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps(stdin) if stdin is not None else "")
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                d2r.main(list(argv))
            return buf.getvalue()
        finally:
            sys.stdin = old

    def add(self, c):
        return self.cli("add", stdin=c)

    def md(self, name="CLAUDE.md"):
        p = os.path.join(self.proj, name)
        return slurp(p) if os.path.exists(p) else None

    def store(self, is_global=False):
        return d2r.load(is_global)

    # tests from the blueprint §4
    def test_add_then_render_is_idempotent(self):
        self.add(card(1))
        first = self.md()
        self.cli("render")
        self.cli("render")
        self.assertEqual(self.md(), first)
        self.assertIn("- [r001] 当情形1 → 做 做1 ｜证据「用户原话1」｜范围 project", first)

    def test_deprecate_keeps_the_line(self):
        self.add(card(1))
        self.add(card(2))
        self.cli("deprecate", "r1", "--reason", "用户说作废")
        rules = self.store()
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["status"], "deprecated")
        self.assertEqual(rules[0]["reason"], "用户说作废")
        self.assertTrue(rules[0]["deprecated_at"])
        self.assertNotIn("[r001]", self.md())  # deprecated is not rendered
        self.assertIn("[r002]", self.md())

    def test_text_outside_block_untouched(self):
        before = "# 我的项目\n\n手写的规矩，一字不能动。\n"
        after = "\n## 尾巴\n也不能动\n"
        with open(os.path.join(self.proj, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write(before + d2r.BEGIN + "\n" + d2r.END + after)
        self.add(card(1))
        self.cli("deprecate", "r001")
        self.add(card(2))
        text = self.md()
        self.assertTrue(text.startswith(before))
        self.assertTrue(text.endswith(after))
        self.assertEqual(text.count(d2r.END), 1)

    def test_all_except_leaves_three_active(self):  # fixture 008 / testset C
        for i in range(1, 11):
            self.add(card(i))
        self.cli("deprecate", "--all-except", "r2,r5,r9")
        rules = self.store()
        self.assertEqual(len(rules), 10)
        active = [r["id"] for r in rules if r["status"] == "active"]
        self.assertEqual(active, ["r002", "r005", "r009"])
        self.assertTrue(all(r["deprecated_at"] for r in rules if r["status"] == "deprecated"))
        self.assertEqual(sorted(l for l in self.md().splitlines() if l.startswith("- [")),
                         [l for l in self.md().splitlines() if l.startswith("- [")])
        for rid in ("r001", "r003", "r010"):
            self.assertNotIn(f"[{rid}]", self.md())

    # extra guards
    def test_all_except_unknown_id_changes_nothing(self):
        self.add(card(1))
        with self.assertRaises(SystemExit):
            self.cli("deprecate", "--all-except", "r7")
        self.assertEqual(self.store()[0]["status"], "active")

    def test_task_scope_is_default_and_not_rendered(self):  # fixture 004 / testset B
        c = card(1)
        del c["scope"]
        self.add(c)
        self.assertEqual(self.store()[0]["scope"], "task")
        self.assertIsNone(self.md())
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "CLAUDE.md")))

    def test_global_goes_to_home(self):
        self.add(card(1, scope="global"))
        g = slurp(os.path.join(self.home, ".claude", "CLAUDE.md"))
        self.assertIn("[g001]", g)
        self.assertIsNone(self.md())
        self.assertEqual(self.store(True)[0]["id"], "g001")

    def test_codex_host_writes_agents_md(self):
        self.cli("--host", "codex", "add", stdin=card(1))
        self.assertIn("[r001]", self.md("AGENTS.md"))
        self.assertIsNone(self.md())

    def test_evidence_required(self):
        for bad in (card(1, evidence={"quote": "  "}), card(1, evidence={})):
            with self.assertRaises(SystemExit):
                self.add(bad)
        self.assertEqual(self.store(), [])

    def test_undo_restores_previous_state(self):
        self.add(card(1))
        self.add(card(2))
        self.cli("undo")
        self.assertEqual([r["id"] for r in self.store()], ["r001"])
        self.assertNotIn("[r002]", self.md())
        self.cli("deprecate", "r1")
        self.cli("undo")
        self.assertEqual(self.store()[0]["status"], "active")
        self.assertIn("[r001]", self.md())
        with self.assertRaises(SystemExit):
            self.cli("undo")  # only one level

    def test_undo_first_add_removes_store(self):
        self.add(card(1))
        self.cli("undo")
        self.assertEqual(self.store(), [])

    def test_supersedes_deprecates_old(self):
        self.add(card(1))
        self.add(card(2, supersedes=["r1"]))
        old, new = self.store()
        self.assertEqual((old["status"], old["reason"]), ("deprecated", "superseded by r002"))
        self.assertEqual(new["supersedes"], ["r001"])

    def test_list_hides_deprecated_unless_all(self):
        self.add(card(1))
        self.add(card(2))
        self.cli("deprecate", "r1")
        self.assertNotIn("[r001]", self.cli("list"))
        self.assertIn("[r001]", self.cli("list", "--all"))

    def test_multiline_quote_renders_on_one_line(self):
        self.add(card(1, evidence={"quote": "第一行\n第二行"}))
        self.assertIn("证据「第一行 第二行」", self.md())
        self.assertEqual(self.store()[0]["evidence"]["quote"], "第一行\n第二行")  # source keeps it verbatim


if __name__ == "__main__":
    unittest.main()
