import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


danmaku = load_module("src.danmaku", ROOT / "src" / "danmaku.py")
import sys
sys.modules["src.danmaku"] = danmaku
douyu = load_module("src.douyu_danmaku", ROOT / "src" / "douyu_danmaku.py")


class DouyuProtocolTests(unittest.TestCase):
    def test_pack_message_header_and_payload(self):
        packet = douyu.pack_message("type@=loginreq/roomid@=123/")
        self.assertGreater(len(packet), 12)
        first_length = int.from_bytes(packet[0:4], "little")
        second_length = int.from_bytes(packet[4:8], "little")
        self.assertEqual(first_length, second_length)
        self.assertEqual(first_length + 4, len(packet))
        self.assertIn(b"type@=loginreq", packet)

    def test_parse_stt_unescapes_values(self):
        parsed = douyu.parse_stt("type@=chatmsg/uid@=42/txt@=hello@Sworld@Aok/")
        self.assertEqual(parsed["type"], "chatmsg")
        self.assertEqual(parsed["uid"], "42")
        self.assertEqual(parsed["txt"], "hello/world@ok")

    def test_numeric_room_url_resolves_without_network(self):
        self.assertEqual(douyu.resolve_room_id("https://www.douyu.com/123456"), "123456")

    def test_shared_duplicate_filter_excludes_repeated_chat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "record.ts"
            session = douyu.DouyuDanmakuSession(
                str(video), "https://www.douyu.com/123", "", duplicate_window=60
            )
            session.message_path.parent.mkdir(parents=True, exist_ok=True)
            session.message_file = session.message_path.open("w", encoding="utf-8")
            session._consume(1, {"type": "chat", "content": "福袋口令"})
            session._consume(2, {"type": "chat", "content": "福袋口令"})
            session.message_file.close()
            session.message_file = None
            self.assertEqual(session.rows[1].comment_count, 1)
            self.assertEqual(session.rows[2].comment_count, 0)
            self.assertEqual(session.unique_chat_count, 1)
            self.assertEqual(session.duplicate_chat_count, 1)
            self.assertEqual(session.message_path.read_text(encoding="utf-8").count("福袋口令"), 1)


if __name__ == "__main__":
    unittest.main()
