import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
import detect  # noqa: E402


def samples():
    with open(os.path.join(HERE, "detect_samples.tsv"), encoding="utf-8") as f:
        for ln in f:
            if ln.strip() and not ln.startswith("#"):
                label, text = ln.rstrip("\n").split("\t", 1)
                yield label, text


class DetectTest(unittest.TestCase):
    def test_negatives_never_hit(self):
        hits = [(t, detect.match(t)) for label, t in samples() if label == "neg" and detect.match(t)]
        self.assertEqual(hits, [])

    def test_positives_hit(self):
        misses = [t for label, t in samples() if label == "pos" and not detect.match(t)]
        self.assertEqual(misses, [])

    def test_sample_counts(self):
        labels = [label for label, _ in samples()]
        self.assertEqual((labels.count("pos"), labels.count("neg")), (20, 20))

    def run_hook(self, prompt, env=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "hooks", "detect.py")],
                              input=json.dumps({"prompt": prompt}), capture_output=True, text=True,
                              env={k: v for k, v in {**os.environ, **(env or {})}.items() if v != ""})

    def test_hook_output_on_hit(self):
        r = self.run_hook("别再给我加 emoji 了")
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertIn("【规则卡", out["additionalContext"])
        self.assertNotIn("description:", out["additionalContext"])  # frontmatter stripped
        self.assertNotIn("$", out["additionalContext"])  # placeholders filled in

    def test_hook_silent_on_miss_in_regex_mode_and_when_off(self):
        self.assertEqual(self.run_hook("帮我看一眼这个文件", {"D2R_MODE": "regex"}).stdout, "")
        self.assertEqual(self.run_hook("别再给我加 emoji 了", {"D2R_OFF": "1"}).stdout, "")
        self.assertEqual(self.run_hook("帮我看一眼这个文件", {"D2R_OFF": "1"}).stdout, "")

    def test_nudge_on_miss_by_default(self):
        out = json.loads(self.run_hook("帮我看一眼这个文件", {"D2R_MODE": ""}).stdout)["hookSpecificOutput"]
        ctx = out["additionalContext"]
        self.assertEqual(ctx, detect.NUDGE.format(root=ROOT))
        self.assertNotIn("四条规矩", ctx)  # the reminder carries the card skeleton, not the whole spec
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "commands", "d2r.md")))
        self.assertLessEqual(len(detect.NUDGE.replace("{root}", "")), 400)

    def test_nudge_mode_still_injects_full_card_on_regex_hit(self):
        ctx = json.loads(self.run_hook("别再给我加 emoji 了")
                         .stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("【规则卡", ctx)

    def test_bad_stdin_is_silent(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "hooks", "detect.py")],
                           input="not json", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
