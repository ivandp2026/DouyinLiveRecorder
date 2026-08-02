import importlib.util
import sys
import tempfile
import json
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
summarize_highlights = danmaku.summarize_highlights
segment_events = danmaku._segment_events
segment_output_path = danmaku._segment_output_path
output_prefix = danmaku.output_prefix
DanmakuSession = danmaku.DanmakuSession


class DanmakuTests(unittest.TestCase):
    def test_segment_output_prefix(self):
        self.assertEqual(
            output_prefix("downloads/a_2026-01-01_%03d.ts"),
            Path("downloads/a_2026-01-01"),
        )

    def test_segment_events_are_shifted_to_each_local_timeline(self):
        events = [
            {"second": 1799.5, "type": "chat", "text": "第一段"},
            {"second": 1800.0, "type": "chat", "text": "第二段开头"},
            {"second": 1805.25, "type": "chat", "text": "第二段"},
            {"second": 3600.0, "type": "chat", "text": "第三段"},
        ]
        shifted = segment_events(events, 1800.0, 1800.0)
        self.assertEqual([item["text"] for item in shifted], ["第二段开头", "第二段"])
        self.assertEqual(shifted[0]["second"], 0.0)
        self.assertEqual(shifted[1]["second"], 5.25)

    def test_segment_output_matches_source_piece(self):
        self.assertEqual(
            segment_output_path(Path("record_001.ts")),
            Path("record_001.danmaku.mp4"),
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

    def test_highlight_content_summary_uses_only_range_chat(self):
        highlights = [{"start": 10, "end": 20}]
        records = [
            {"type": "chat", "second": 9, "text": "区间外内容", "user_id": "0"},
            {"type": "chat", "second": 12, "text": "华东太强了", "user_id": "1"},
            {"type": "chat", "second": 14, "text": "华东太强了", "user_id": "2"},
            {"type": "chat", "second": 18, "text": "享受比赛", "user_id": "1"},
            {"type": "gift", "second": 15, "text": "火箭", "user_id": "3"},
        ]
        result = summarize_highlights(records, highlights)[0]
        self.assertEqual(result["comment_count"], 3)
        self.assertEqual(result["unique_users"], 2)
        self.assertIn("华东太强了", result["content_summary"])
        self.assertNotIn("区间外内容", result["content_summary"])
        self.assertNotIn("火箭", result["content_summary"])

    def test_report_includes_hover_seconds_and_content_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.html"
            danmaku._write_report(report, {
                "summary": {"title": "测试报告"},
                "timeline": [{"second": 40, "heat_score": 12, "raw_comment_count": 3}],
                "highlights": [{"content_summary": "主要讨论比赛"}],
            })
            content = report.read_text(encoding="utf-8")
            self.assertIn("鼠标移到曲线上可查看准确秒数", content)
            self.assertIn("内容总结", content)
            self.assertIn("p.second", content)

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

    def test_stop_writes_sidecars_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            video = str(Path(directory) / "record.ts")
            session = DanmakuSession(video, "https://live.douyin.com/123", "")
            session._consume(0, {"method": "WebcastChatMessage", "user": {"id": "1"}})
            first = session.stop()
            second = session.stop()
            self.assertEqual(first, second)
            self.assertTrue(first[0].is_file())
            self.assertTrue(first[1].is_file())
            payload = json.loads(first[1].read_text(encoding="utf-8"))
            self.assertIn("collector", payload)
            self.assertEqual(payload["collector"]["raw_event_count"], 0)

    def test_duplicate_chat_is_not_saved_or_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            session = DanmakuSession(
                str(Path(directory) / "record.ts"),
                "https://live.douyin.com/123", "", duplicate_window=60,
            )
            session.message_path.parent.mkdir(parents=True, exist_ok=True)
            session.message_file = session.message_path.open("w", encoding="utf-8", buffering=1)
            event = {"method": "WebcastChatMessage", "content": "  福袋口令   666  "}
            session._consume(10, event)
            session._consume(20, event)
            session._consume(71, event)
            session.stop()
            lines = session.message_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, ["[00:00:10] 福袋口令 666", "[00:01:11] 福袋口令 666"])
            self.assertEqual(sum(row.comment_count for row in session.rows), 2)
            self.assertEqual(session.duplicate_chat_count, 1)


if __name__ == "__main__":
    unittest.main()
