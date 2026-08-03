import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.danmaku import (
    SecondStats,
    _compute_heat,
    _keyword_tokens,
    _probe_duration,
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

    def test_probe_duration_retries_until_last_segment_is_finalized(self):
        unfinished = subprocess.CompletedProcess([], 1, "", "Duration: N/A")
        finished = subprocess.CompletedProcess([], 1, "", "Duration: 00:30:00.25")
        with patch("src.danmaku.subprocess.run", side_effect=[unfinished, finished]) as run:
            with patch("src.danmaku.time.sleep") as sleep:
                duration = _probe_duration(
                    "ffmpeg", Path("live_001.ts"), attempts=2, retry_delay=0.01
                )
        self.assertEqual(duration, 1800.25)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_keywords_ignore_common_noise(self):
        words = _keyword_tokens("这个主播真的可以，精彩操作太厉害了")
        self.assertIn("精彩操作太厉害了", words)
        self.assertNotIn("这个", words)


if __name__ == "__main__":
    unittest.main()
