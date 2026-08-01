"""Danmaku timeline recording and highlight detection.

The Douyin wire protocol changes frequently, so this module deliberately
separates transport from analytics.  A collector process writes one JSON
object per line to stdout and this module aligns those events with FFmpeg's
recording clock.
"""

from __future__ import annotations

import csv
import atexit
import base64
import json
import os
import queue
import re
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlparse


CHAT_TYPES = {"chat", "comment", "WebcastChatMessage"}
GIFT_TYPES = {"gift", "WebcastGiftMessage"}
LIKE_TYPES = {"like", "WebcastLikeMessage"}

_relay_lock = threading.Lock()
_relay_process: subprocess.Popen | None = None
_sessions_lock = threading.Lock()
_active_sessions: set["DanmakuSession"] = set()


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_douyin_relay(executable: str, port: int) -> None:
    """Start the bundled MIT-licensed douyinLive relay when necessary."""
    global _relay_process
    with _relay_lock:
        if _port_is_open(port):
            return
        path = Path(executable)
        if not path.is_file():
            raise FileNotFoundError(f"未找到抖音弹幕组件: {path}")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        _relay_process = subprocess.Popen(
            [str(path), "--port", str(port), "--log-level", "warn"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _port_is_open(port):
                return
            if _relay_process.poll() is not None:
                break
            time.sleep(0.2)
        raise RuntimeError("抖音弹幕组件启动失败或端口不可用")


def _stop_relay() -> None:
    if _relay_process and _relay_process.poll() is None:
        _relay_process.terminate()


atexit.register(_stop_relay)


@dataclass
class SecondStats:
    second: int
    comment_count: int = 0
    unique_users: int = 0
    gift_count: int = 0
    like_count: int = 0


def output_prefix(video_path: str) -> Path:
    """Return a stable sidecar prefix, including for FFmpeg segment paths."""
    path = Path(video_path)
    stem = re.sub(r"_%0\d+d$", "", path.stem)
    return path.with_name(stem)


def detect_highlights(
    rows: Iterable[SecondStats], *, window: int = 10, baseline_window: int = 60,
    multiplier: float = 2.5, min_comments: int = 5, pre_roll: int = 15,
    post_roll: int = 25, merge_gap: int = 10,
) -> list[dict]:
    """Find sustained comment spikes using a trailing adaptive baseline."""
    values = list(rows)
    if not values:
        return []
    counts = [row.comment_count for row in values]
    hot: list[int] = []
    for index in range(len(counts)):
        start = max(0, index - window + 1)
        current = counts[start:index + 1]
        history_start = max(0, start - baseline_window)
        history = counts[history_start:start]
        baseline = (sum(history) / len(history)) if history else 0.0
        current_avg = sum(current) / len(current)
        threshold = max(float(min_comments), baseline * multiplier)
        if len(current) == window and current_avg >= threshold:
            hot.append(index)
    if not hot:
        return []

    ranges: list[list[int]] = []
    for second in hot:
        candidate = [max(0, second - window + 1 - pre_roll), min(len(values) - 1, second + post_roll)]
        if ranges and candidate[0] <= ranges[-1][1] + merge_gap:
            ranges[-1][1] = max(ranges[-1][1], candidate[1])
        else:
            ranges.append(candidate)

    result = []
    for start, end in ranges:
        peak = max(range(start, end + 1), key=lambda i: counts[i])
        result.append({
            "start": start,
            "end": end,
            "duration": end - start + 1,
            "peak_second": peak,
            "peak_comments": counts[peak],
            "total_comments": sum(counts[start:end + 1]),
        })
    return result


class DanmakuSession:
    """Run a line-oriented collector and write recording sidecars."""

    def __init__(self, video_path: str, room_url: str, cookie: str, command: str = "",
                 highlight_options: dict | None = None, relay_executable: str = "",
                 relay_port: int = 1088):
        self.prefix = output_prefix(video_path)
        self.room_url = room_url
        self.cookie = cookie
        self.command = command
        self.relay_executable = relay_executable
        self.relay_port = relay_port
        self.highlight_options = highlight_options or {}
        self.started_at = time.monotonic()
        self.stop_event = threading.Event()
        self.events: queue.Queue[tuple[int, dict]] = queue.Queue()
        self.rows: list[SecondStats] = []
        self.users: dict[int, set[str]] = {}
        self.process: subprocess.Popen | None = None
        self.websocket = None
        self.reader_thread: threading.Thread | None = None
        self.writer_thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()
        self._stop_result: tuple[Path, Path] | None = None

    def start(self) -> None:
        if not self.command:
            ensure_douyin_relay(self.relay_executable, self.relay_port)
            self.reader_thread = threading.Thread(target=self._read_relay_events, daemon=True)
            self.writer_thread = threading.Thread(target=self._aggregate, daemon=True)
            self.reader_thread.start()
            self.writer_thread.start()
            with _sessions_lock:
                _active_sessions.add(self)
            return
        args = shlex.split(self.command, posix=os.name != "nt")
        if not args:
            raise ValueError("弹幕采集命令为空")
        env = os.environ.copy()
        env["DOUYIN_ROOM_URL"] = self.room_url
        env["DOUYIN_COOKIE"] = self.cookie
        self.process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", env=env,
        )
        self.reader_thread = threading.Thread(target=self._read_events, daemon=True)
        self.writer_thread = threading.Thread(target=self._aggregate, daemon=True)
        self.reader_thread.start()
        self.writer_thread.start()
        with _sessions_lock:
            _active_sessions.add(self)

    def _relay_url(self) -> str:
        room_id = urlparse(self.room_url).path.strip("/").split("/")[-1]
        if not room_id:
            raise ValueError("无法从链接取得抖音直播间标识")
        url = f"ws://127.0.0.1:{self.relay_port}/ws/{quote(room_id)}"
        if self.cookie:
            cookie_b64 = base64.urlsafe_b64encode(self.cookie.encode()).decode().rstrip("=")
            url += f"?cookie_b64={quote(cookie_b64)}"
        return url

    def _read_relay_events(self) -> None:
        import websocket

        while not self.stop_event.is_set():
            try:
                self.websocket = websocket.create_connection(
                    self._relay_url(), timeout=10, enable_multithread=True,
                )
                self.websocket.settimeout(1)
                last_ping = time.monotonic()
                while not self.stop_event.is_set():
                    try:
                        message = self.websocket.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.monotonic() - last_ping >= 25:
                            self.websocket.send("ping")
                            last_ping = time.monotonic()
                        continue
                    if not message or message == "pong":
                        continue
                    event = json.loads(message)
                    if isinstance(event, dict) and event.get("type") != "system":
                        second = max(0, int(time.monotonic() - self.started_at))
                        self.events.put((second, event))
            except Exception:
                if not self.stop_event.wait(2):
                    continue
            finally:
                if self.websocket:
                    try:
                        self.websocket.close()
                    except Exception:
                        pass
                    self.websocket = None

    def _read_events(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            if self.stop_event.is_set():
                break
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    second = max(0, int(time.monotonic() - self.started_at))
                    self.events.put((second, event))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

    def _ensure_row(self, second: int) -> SecondStats:
        while len(self.rows) <= second:
            self.rows.append(SecondStats(second=len(self.rows)))
        return self.rows[second]

    def _consume(self, second: int, event: dict) -> None:
        row = self._ensure_row(second)
        event_type = str(event.get("type") or event.get("method") or "")
        if event_type in CHAT_TYPES:
            row.comment_count += 1
            user = event.get("user") if isinstance(event.get("user"), dict) else {}
            user_id = (event.get("user_id") or event.get("userId") or event.get("nickname")
                       or user.get("id") or user.get("idStr") or user.get("nickname"))
            if user_id is not None:
                users = self.users.setdefault(second, set())
                users.add(str(user_id))
                row.unique_users = len(users)
        elif event_type in GIFT_TYPES:
            row.gift_count += max(1, int(event.get("count", event.get("repeatCount", 1)) or 1))
        elif event_type in LIKE_TYPES:
            row.like_count += max(1, int(event.get("count", 1) or 1))

    def _aggregate(self) -> None:
        while not self.stop_event.is_set() or not self.events.empty():
            try:
                second, event = self.events.get(timeout=0.2)
                self._consume(second, event)
            except queue.Empty:
                elapsed = max(0, int(time.monotonic() - self.started_at))
                self._ensure_row(elapsed)

    def stop(self) -> tuple[Path, Path]:
        with self._stop_lock:
            if self._stop_result:
                return self._stop_result
            self.stop_event.set()
            if self.websocket:
                try:
                    self.websocket.close()
                except Exception:
                    pass
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            if self.reader_thread:
                self.reader_thread.join(timeout=2)
            if self.writer_thread:
                self.writer_thread.join(timeout=2)
            self._ensure_row(max(0, int(time.monotonic() - self.started_at)))
            csv_path = self.prefix.with_suffix(".danmaku.csv")
            json_path = self.prefix.with_suffix(".highlights.json")
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(asdict(SecondStats(0)).keys()))
                writer.writeheader()
                writer.writerows(asdict(row) for row in self.rows)
            payload = {
                "video": str(self.prefix),
                "duration_seconds": len(self.rows),
                "highlights": detect_highlights(self.rows, **self.highlight_options),
            }
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._stop_result = (csv_path, json_path)
            with _sessions_lock:
                _active_sessions.discard(self)
            return self._stop_result


def _finalize_active_sessions() -> None:
    """Flush sidecars when the recorder exits before FFmpeg's normal callback."""
    with _sessions_lock:
        sessions = list(_active_sessions)
    for session in sessions:
        try:
            session.stop()
        except Exception:
            pass


atexit.register(_finalize_active_sessions)
