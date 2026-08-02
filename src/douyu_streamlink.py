"""Douyu video resolver backed by Streamlink's maintained Douyu plugin."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from streamlink import Streamlink


QUALITY_ORDER = {
    "OD": ("best",),
    "BD": ("best",),
    "UHD": ("1080p60", "1080p", "best"),
    "HD": ("720p60", "720p", "best"),
    "SD": ("480p", "360p", "worst"),
    "LD": ("360p", "worst"),
}

DOUYU_RECONNECT_DELAYS = (3, 5, 10, 20, 30)
VIDEO_EXTENSIONS = {".ts", ".flv", ".mkv", ".mov", ".m4v", ".webm"}


def normalize_douyu_url(url: str) -> str:
    parsed = urlparse(url.strip())
    room = parsed.path.strip("/").split("/")[0]
    if not room:
        raise ValueError(f"无法从斗鱼链接提取房间号: {url}")
    return f"https://www.douyu.com/{room}"


def reconnect_output_path(path: str, reconnect_index: int) -> str:
    """Return a new output path without overwriting an earlier recording part."""
    source = Path(path)
    marker = f"_reconnect{reconnect_index:03d}"
    if "%03d" in source.name:
        name = source.name.replace("%03d", f"reconnect{reconnect_index:03d}_%03d", 1)
    else:
        name = f"{source.stem}{marker}{source.suffix}"
    return str(source.with_name(name))


def refreshed_ffmpeg_command(command: list[str], stream_url: str, output_path: str) -> list[str]:
    """Copy an FFmpeg command and replace only its input URL and output path."""
    refreshed = list(command)
    try:
        input_index = refreshed.index("-i") + 1
    except (ValueError, IndexError) as exc:
        raise ValueError("FFmpeg 命令缺少输入参数 -i") from exc
    refreshed[input_index] = stream_url
    refreshed[-1] = output_path
    return refreshed


def ffmpeg_proxy(command: list[str]) -> str:
    """Read the configured HTTP proxy from an FFmpeg command, if present."""
    try:
        proxy_index = command.index("-http_proxy") + 1
        return command[proxy_index]
    except (ValueError, IndexError):
        return ""


def resolve_douyu_stream(url: str, quality: str = "OD", cookies: str = "", proxy: str = "") -> dict:
    normalized = normalize_douyu_url(url)
    session = Streamlink()
    if proxy:
        session.set_option("http-proxy", proxy)
    if cookies:
        session.set_option("http-headers", {"Cookie": cookies})

    # Streamlink 8.x returns (plugin_name, plugin_class, resolved_url), not a
    # plugin instance. Instantiate the plugin explicitly so metadata and
    # streams come from the same resolved URL.
    _plugin_name, plugin_class, resolved_url = session.resolve_url(normalized)
    plugin = plugin_class(session, resolved_url, None)
    streams = plugin.streams()
    if not streams:
        return {
            "anchor_name": normalized.rsplit("/", 1)[-1],
            "is_live": False,
            "title": "",
        }

    selected = None
    for name in QUALITY_ORDER.get(quality, ("best",)):
        if name in streams:
            selected = streams[name]
            break
    if selected is None:
        selected = streams.get("best") or next(iter(streams.values()))

    stream_url = selected.to_url()
    if not stream_url:
        raise RuntimeError("Streamlink 已识别斗鱼直播间，但没有返回可录制的流地址")

    metadata = {}
    try:
        metadata = plugin.get_metadata() or {}
    except Exception:
        pass

    room_id = normalized.rsplit("/", 1)[-1]
    return {
        "anchor_name": str(metadata.get("author") or room_id),
        "is_live": True,
        "title": str(metadata.get("title") or "斗鱼直播"),
        "quality": quality,
        "record_url": stream_url,
        "flv_url": stream_url,
    }
