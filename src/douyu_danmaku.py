"""Douyu live chat collector using the public barrage socket protocol."""
from __future__ import annotations

import re
import socket
import struct
import threading
import time
from urllib.parse import urlparse

import requests

from src.danmaku import DanmakuSession, _active_sessions, _sessions_lock


SERVER_HOST = "danmuproxy.douyu.com"
SERVER_PORT = 8601


def _escape(value: str) -> str:
    return value.replace("@", "@A").replace("/", "@S")


def _unescape(value: str) -> str:
    return value.replace("@S", "/").replace("@A", "@")


def pack_message(payload: str) -> bytes:
    body = payload.encode("utf-8") + b"\x00"
    length = len(body) + 8
    return struct.pack("<IIHBB", length, length, 689, 0, 0) + body


def parse_stt(payload: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in payload.rstrip("\x00/").split("/"):
        if "@=" not in field:
            continue
        key, value = field.split("@=", 1)
        result[_unescape(key)] = _unescape(value)
    return result


def resolve_room_id(room_url: str, timeout: int = 10) -> str:
    slug = urlparse(room_url).path.strip("/").split("/")[0]
    if slug.isdigit():
        return slug
    response = requests.get(
        room_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36"
            )
        },
        timeout=timeout,
    )
    response.raise_for_status()
    patterns = (
        r'"room_id"\s*:\s*"?(\d+)"?',
        r"\$ROOM\.room_id\s*=\s*(\d+)",
        r"roomId\s*[:=]\s*[\"']?(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, response.text)
        if match:
            return match.group(1)
    raise ValueError(f"无法解析斗鱼真实房间号: {room_url}")


class DouyuDanmakuSession(DanmakuSession):
    """Collect Douyu chat and feed the shared raw-data/heat engine."""

    def start(self) -> None:
        self._open_outputs()
        self.reader_thread = threading.Thread(
            target=self._read_douyu_events, daemon=True
        )
        self.writer_thread = threading.Thread(target=self._aggregate, daemon=True)
        self.reader_thread.start()
        self.writer_thread.start()
        with _sessions_lock:
            _active_sessions.add(self)

    def _send(self, sock: socket.socket, payload: str) -> None:
        sock.sendall(pack_message(payload))

    def _heartbeat(self, sock: socket.socket) -> None:
        while not self.stop_event.wait(40):
            try:
                self._send(sock, f"type@=mrkl/tick@={int(time.time())}/")
            except OSError:
                return

    def _read_douyu_events(self) -> None:
        while not self.stop_event.is_set():
            sock: socket.socket | None = None
            try:
                room_id = resolve_room_id(self.room_url)
                sock = socket.create_connection(
                    (SERVER_HOST, SERVER_PORT), timeout=10
                )
                sock.settimeout(1)
                self._send(sock, f"type@=loginreq/roomid@={_escape(room_id)}/")
                self._send(
                    sock,
                    f"type@=joingroup/rid@={_escape(room_id)}/gid@=-9999/",
                )
                self.connected = True
                heartbeat = threading.Thread(
                    target=self._heartbeat, args=(sock,), daemon=True
                )
                heartbeat.start()
                buffer = bytearray()
                while not self.stop_event.is_set():
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise ConnectionError("斗鱼弹幕连接已关闭")
                    buffer.extend(chunk)
                    while len(buffer) >= 12:
                        packet_length = struct.unpack_from("<I", buffer, 0)[0]
                        total_length = packet_length + 4
                        if packet_length < 8 or total_length > 8 * 1024 * 1024:
                            del buffer[0]
                            continue
                        if len(buffer) < total_length:
                            break
                        payload = (
                            bytes(buffer[12:total_length])
                            .rstrip(b"\x00")
                            .decode("utf-8", "replace")
                        )
                        del buffer[:total_length]
                        message = parse_stt(payload)
                        message_type = message.get("type", "")
                        second = max(
                            0, int(time.monotonic() - self.started_at)
                        )
                        if message_type == "chatmsg":
                            self.raw_event_count += 1
                            self.events.put(
                                (
                                    second,
                                    {
                                        "type": "chat",
                                        "content": message.get("txt", ""),
                                        "user_id": message.get("uid", ""),
                                        "nickname": message.get("nn", ""),
                                        "level": message.get("level", ""),
                                        "badge_name": message.get("bnn", ""),
                                        "badge_level": message.get("bl", ""),
                                        "color": message.get("col", ""),
                                        "douyu_message": message,
                                    },
                                )
                            )
                        elif message_type in {"dgb", "spbc"}:
                            self.raw_event_count += 1
                            self.events.put(
                                (
                                    second,
                                    {
                                        "type": "gift",
                                        "count": message.get("gfcnt", 1),
                                        "gift_id": message.get("gfid", ""),
                                        "user_id": message.get("uid", ""),
                                        "nickname": message.get("nn", ""),
                                        "douyu_message": message,
                                    },
                                )
                            )
                        elif message_type in {"uenter", "rss"}:
                            self.raw_event_count += 1
                            self.events.put(
                                (
                                    second,
                                    {
                                        "type": "system",
                                        "user_id": message.get("uid", ""),
                                        "nickname": message.get("nn", ""),
                                        "douyu_message": message,
                                    },
                                )
                            )
            except Exception as exc:
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self.stop_event.wait(3):
                    break
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
