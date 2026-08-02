"""Douyu video resolver backed by Streamlink's maintained Douyu plugin."""
from __future__ import annotations

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


def normalize_douyu_url(url: str) -> str:
    parsed = urlparse(url.strip())
    room = parsed.path.strip("/").split("/")[0]
    if not room:
        raise ValueError(f"无法从斗鱼链接提取房间号: {url}")
    return f"https://www.douyu.com/{room}"


def resolve_douyu_stream(url: str, quality: str = "OD", cookies: str = "", proxy: str = "") -> dict:
    normalized = normalize_douyu_url(url)
    session = Streamlink()
    if proxy:
        session.set_option("http-proxy", proxy)
        session.set_option("https-proxy", proxy)
    if cookies:
        session.set_option("http-headers", {"Cookie": cookies})

    plugin = session.resolve_url(normalized)
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
