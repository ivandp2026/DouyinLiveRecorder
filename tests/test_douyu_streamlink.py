import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


# Helper tests do not require the third-party package to be installed locally.
try:
    import streamlink  # noqa: F401
except ModuleNotFoundError:
    streamlink_stub = types.ModuleType("streamlink")
    streamlink_stub.Streamlink = object
    sys.modules["streamlink"] = streamlink_stub

spec = importlib.util.spec_from_file_location(
    "douyu_streamlink_under_test", ROOT / "src" / "douyu_streamlink.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class DouyuReconnectHelperTests(unittest.TestCase):
    def test_reconnect_path_preserves_all_previous_single_file_parts(self):
        self.assertEqual(
            module.reconnect_output_path("C:/record/live.ts", 2),
            "C:/record/live_reconnect002.ts",
        )

    def test_reconnect_path_keeps_segment_pattern_unique(self):
        self.assertEqual(
            module.reconnect_output_path("C:/record/live_%03d.ts", 3),
            "C:/record/live_reconnect003_%03d.ts",
        )

    def test_refresh_replaces_input_and_output_without_mutating_original(self):
        original = ["ffmpeg", "-reconnect", "1", "-i", "old-url", "-f", "mpegts", "old.ts"]
        refreshed = module.refreshed_ffmpeg_command(original, "new-url", "new.ts")
        self.assertEqual(original[4], "old-url")
        self.assertEqual(original[-1], "old.ts")
        self.assertEqual(refreshed[4], "new-url")
        self.assertEqual(refreshed[-1], "new.ts")

    def test_proxy_is_reused_when_refreshing_streamlink_url(self):
        command = ["ffmpeg", "-http_proxy", "http://127.0.0.1:9674", "-i", "url", "out.ts"]
        self.assertEqual(module.ffmpeg_proxy(command), "http://127.0.0.1:9674")
        self.assertEqual(module.ffmpeg_proxy(["ffmpeg", "-i", "url", "out.ts"]), "")


if __name__ == "__main__":
    unittest.main()
