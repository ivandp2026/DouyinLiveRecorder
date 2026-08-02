import tempfile
import unittest
from pathlib import Path

from src.danmaku import (
    SecondStats,
    _compute_heat,
    _keyword_tokens,
    build_ass,
    detect_highlights,
    output_prefix,
)


class DanmakuOutputTests(unittest.TestCase):
    def test_output_prefix_removes_segment_template(self):
        self.assertEqual(
            str(output_prefix("recordings/live_%03d.ts")),
            str(Path("recordings/live")),
        )

    def test_heat_penalizes_duplicates_but_keeps_raw_count(self):
        row = SecondStats(
            second=0,
            raw_comment_count=10,
            comment_count=4,
            duplicate_count=6,
            unique_users=3,
        )
        _compute_heat([row])
        self.assertEqual(row.repeat_ratio, 0.6)
        self.assertGreater(row.heat_score, 0)

    def test_highlights_include_editing_times(self):
        rows = [SecondStats(second=i) for i in range(90)]
        for i in range(40, 55):
            rows[i].raw_comment_count = 20
            rows[i].comment_count = 12
            rows[i].unique_users = 8
        highlights = detect_highlights(
            rows, window=5, baseline_window=20, min_comments=3
        )
        self.assertTrue(highlights)
        self.assertIn("peak_time", highlights[0])
        self.assertGreater(highlights[0]["total_comments"], 0)

    def test_ass_keeps_readable_subset(self):
        events = [
            {
                "sequence": index,
                "second": 1,
                "type": "chat",
                "text": f"测试弹幕 {index}",
            }
            for index in range(30)
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.ass"
            _, count = build_ass(events, path, max_per_second=8)
            self.assertEqual(count, 8)
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("[Events]", text)
            self.assertIn("Dialogue:", text)

    def test_keywords_ignore_common_noise(self):
        words = _keyword_tokens("这个主播真的可以，精彩操作太厉害了")
        self.assertIn("精彩操作太厉害了", words)
        self.assertNotIn("这个", words)


if __name__ == "__main__":
    unittest.main()
