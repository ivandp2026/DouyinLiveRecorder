import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "src" / "danmaku.py"
SPEC = importlib.util.spec_from_file_location("danmaku_under_test", MODULE_PATH)
danmaku = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = danmaku
SPEC.loader.exec_module(danmaku)
SecondStats = danmaku.SecondStats
detect_highlights = danmaku.detect_highlights
output_prefix = danmaku.output_prefix
DanmakuSession = danmaku.DanmakuSession


class DanmakuTests(unittest.TestCase):
    def test_segment_output_prefix(self):
        self.assertEqual(
            output_prefix("downloads/a_2026-01-01_%03d.ts"),
            Path("downloads/a_2026-01-01"),
        )

    def test_detects_and_expands_spike(self):
        rows = [SecondStats(i, comment_count=1) for i in range(100)]
        for index in range(60, 75):
            rows[index].comment_count = 12
        highlights = detect_highlights(
            rows, window=5, baseline_window=30, multiplier=2.5,
            min_comments=5, pre_roll=10, post_roll=10, merge_gap=5,
        )
        self.assertEqual(len(highlights), 1)
        self.assertLessEqual(highlights[0]["start"], 50)
        self.assertGreaterEqual(highlights[0]["end"], 75)
        self.assertEqual(highlights[0]["peak_comments"], 12)

    def test_no_false_positive_for_steady_chat(self):
        rows = [SecondStats(i, comment_count=3) for i in range(120)]
        self.assertEqual(detect_highlights(rows, min_comments=5), [])

    def test_relay_url_contains_room_and_encoded_cookie(self):
        session = DanmakuSession(
            "video.ts", "https://live.douyin.com/123456", "a=b; c=d",
            relay_port=1099,
        )
        url = session._relay_url()
        self.assertTrue(url.startswith("ws://127.0.0.1:1099/ws/123456?cookie_b64="))
        self.assertNotIn("a=b", url)

    def test_consumes_relay_message_schema(self):
        session = DanmakuSession("video.ts", "https://live.douyin.com/123", "")
        session._consume(0, {
            "method": "WebcastChatMessage",
            "user": {"id": "42", "nickname": "测试用户"},
            "content": "精彩",
        })
        session._consume(0, {
            "method": "WebcastChatMessage",
            "user": {"id": "42"},
            "content": "再来一条",
        })
        self.assertEqual(session.rows[0].comment_count, 2)
        self.assertEqual(session.rows[0].unique_users, 1)


if __name__ == "__main__":
    unittest.main()
