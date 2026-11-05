import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
import detect  # noqa: E402

FIXTURE = os.path.join(ROOT, "fixtures", "real_corrections_1104.jsonl")
HELD_OUT = os.path.join(ROOT, "fixtures", "real_corrections_1104_heldout.jsonl")  # regex misses all six; nudge's job


def cases(path=FIXTURE):
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


class FishRealTalkTest(unittest.TestCase):
    """Real corrections from the 1104 dogfood run: misses cost more than nags, nags are capped."""

    def test_counts(self):
        c = cases()
        self.assertEqual((sum(x["should_fire"] for x in c), sum(not x["should_fire"] for x in c)), (12, 14))

    def test_fires_on_at_least_10_of_12(self):
        hit = [x["text"] for x in cases() if x["should_fire"] and detect.match(x["text"])]
        self.assertGreaterEqual(len(hit), 10)

    def test_nags_at_most_2_of_14(self):
        nag = [x["text"] for x in cases() if not x["should_fire"] and detect.match(x["text"])]
        self.assertLessEqual(len(nag), 2, nag)


class NudgeModeTest(unittest.TestCase):
    """Offline we can only check that every message reaches the model; whether it cards is a live test."""

    def setUp(self):
        self.env = {k: os.environ.pop(k) for k in ("D2R_MODE", "D2R_OFF") if k in os.environ}

    def tearDown(self):
        os.environ.pop("D2R_MODE", None)
        os.environ.update(self.env)

    def test_every_fish_line_gets_card_or_nudge(self):
        for x in cases() + cases(HELD_OUT):
            ctx = detect.context(x["text"])
            self.assertIsNotNone(ctx, x["text"])
            if detect.match(x["text"]):
                self.assertIn("【规则卡", ctx)
            else:
                self.assertEqual(ctx, detect.NUDGE.format(root=detect.ROOT))

    def test_held_out_six_are_regex_misses(self):
        os.environ["D2R_MODE"] = "regex"
        self.assertEqual([x["text"] for x in cases(HELD_OUT) if detect.context(x["text"])], [])


if __name__ == "__main__":
    unittest.main()
