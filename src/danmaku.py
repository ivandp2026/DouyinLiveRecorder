"""Danmaku recording, heat analysis, reports, and optional burn-in video generation.

The transport layer (Douyin relay or Douyu socket client) feeds normalized event
objects into :class:`DanmakuSession`.  The session always preserves raw events,
builds a per-second heat timeline, detects highlight ranges, writes a standalone
HTML report, creates ASS subtitles, and renders a second video with danmaku while
leaving the original recording untouched.
"""
from __future__ import annotations

import atexit
import base64
import csv
import html
import json
import math
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlparse


CHAT_TYPES = {"chat", "comment", "WebcastChatMessage"}
GIFT_TYPES = {"gift", "WebcastGiftMessage"}
LIKE_TYPES = {"like", "WebcastLikeMessage"}
VIDEO_EXTENSIONS = {".mp4", ".ts", ".flv", ".mkv", ".mov", ".m4v"}

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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
    raw_comment_count: int = 0
    comment_count: int = 0
    duplicate_count: int = 0
    unique_users: int = 0
    gift_count: int = 0
    like_count: int = 0
    repeat_ratio: float = 0.0
    heat_score: float = 0.0


def output_prefix(video_path: str) -> Path:
    """Return a stable sidecar prefix, including for FFmpeg segment paths."""
    path = Path(video_path)
    stem = re.sub(r"_%0\d+d$", "", path.stem)
    return path.with_name(stem)


def _clock(second: int | float) -> str:
    value = max(0, int(second))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _ass_clock(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remainder = seconds % 60
    return f"{hours}:{minutes:02d}:{remainder:05.2f}"


def _safe_json(value: object) -> object:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _safe_json(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_safe_json(v) for v in value]
        return str(value)


def _event_user(event: dict) -> tuple[str, str]:
    user = event.get("user") if isinstance(event.get("user"), dict) else {}
    user_id = (
        event.get("user_id")
        or event.get("userId")
        or user.get("id")
        or user.get("idStr")
        or ""
    )
    nickname = (
        event.get("nickname")
        or event.get("user_name")
        or user.get("nickname")
        or user.get("name")
        or user_id
        or ""
    )
    return str(user_id or ""), str(nickname or "")


def _event_type(event: dict) -> str:
    return str(event.get("type") or event.get("method") or "")


def _event_content(event: dict) -> str:
    return " ".join(str(event.get("content") or event.get("text") or "").split())


def _compute_heat(rows: list[SecondStats]) -> None:
    """Calculate transparent, editable heat scores for every second."""
    for row in rows:
        row.repeat_ratio = (
            round(row.duplicate_count / row.raw_comment_count, 4)
            if row.raw_comment_count
            else 0.0
        )
        score = (
            row.comment_count
            + row.unique_users * 1.5
            + row.gift_count * 4.0
            + min(row.like_count, 500) * 0.03
            + row.raw_comment_count * 0.2
            - row.duplicate_count * 0.15
        )
        row.heat_score = round(max(0.0, score), 3)


def detect_highlights(
    rows: Iterable[SecondStats],
    *,
    window: int = 10,
    baseline_window: int = 60,
    multiplier: float = 2.5,
    min_comments: int = 5,
    pre_roll: int = 15,
    post_roll: int = 25,
    merge_gap: int = 10,
) -> list[dict]:
    """Find sustained heat spikes using a trailing adaptive baseline."""
    values = list(rows)
    if not values:
        return []
    _compute_heat(values)
    scores = [row.heat_score for row in values]
    raw_counts = [row.raw_comment_count or row.comment_count for row in values]
    hot: list[int] = []
    for index in range(len(scores)):
        start = max(0, index - window + 1)
        current = scores[start : index + 1]
        history_start = max(0, start - baseline_window)
        history = scores[history_start:start]
        baseline = (sum(history) / len(history)) if history else 0.0
        current_avg = sum(current) / len(current)
        threshold = max(float(min_comments), baseline * multiplier)
        if len(current) == window and current_avg >= threshold:
            hot.append(index)
    if not hot:
        return []

    ranges: list[list[int]] = []
    for second in hot:
        candidate = [
            max(0, second - window + 1 - pre_roll),
            min(len(values) - 1, second + post_roll),
        ]
        if ranges and candidate[0] <= ranges[-1][1] + merge_gap:
            ranges[-1][1] = max(ranges[-1][1], candidate[1])
        else:
            ranges.append(candidate)

    result = []
    for start, end in ranges:
        peak = max(range(start, end + 1), key=lambda i: scores[i])
        result.append(
            {
                "start": start,
                "start_time": _clock(start),
                "end": end,
                "end_time": _clock(end),
                "duration": end - start + 1,
                "peak_second": peak,
                "peak_time": _clock(peak),
                "peak_comments": raw_counts[peak],
                "peak_heat_score": scores[peak],
                "total_comments": sum(raw_counts[start : end + 1]),
                "average_heat_score": round(
                    sum(scores[start : end + 1]) / max(1, end - start + 1), 3
                ),
            }
        )
    return result


def _ass_escape(text: str) -> str:
    text = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    return text.replace("\n", " ").replace("\r", " ")


def build_ass(
    events: Iterable[dict],
    output_path: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    font_size: int = 38,
    duration: float = 9.0,
    max_per_second: int = 12,
) -> tuple[Path, int]:
    """Create a readable scrolling ASS track while preserving raw JSONL."""
    lane_height = font_size + 12
    lane_count = max(6, int((height * 0.72) // lane_height))
    lane_next = [0.0] * lane_count
    per_second: Counter[int] = Counter()
    last_text_second: dict[str, int] = {}
    dialogues: list[str] = []

    sorted_events = sorted(
        (event for event in events if event.get("type") in CHAT_TYPES),
        key=lambda item: (float(item.get("second", 0)), int(item.get("sequence", 0))),
    )
    for event in sorted_events:
        start = max(0.0, float(event.get("second", 0)))
        second_bucket = int(start)
        if per_second[second_bucket] >= max_per_second:
            continue
        text = _ass_escape(str(event.get("text") or event.get("content") or "").strip())
        if not text:
            continue
        previous = last_text_second.get(text)
        if previous is not None and second_bucket - previous <= 8:
            continue
        last_text_second[text] = second_bucket
        per_second[second_bucket] += 1

        lane = min(range(lane_count), key=lambda idx: lane_next[idx])
        start = max(start, lane_next[lane])
        estimated_width = max(font_size * 2, len(text) * font_size * 0.9)
        clear_delay = duration * min(0.85, (estimated_width + 80) / (width + estimated_width))
        lane_next[lane] = start + max(0.7, clear_delay)
        y = 28 + lane * lane_height
        end = start + duration
        dialogues.append(
            "Dialogue: 0,"
            f"{_ass_clock(start)},{_ass_clock(end)},Danmaku,,0,0,0,,"
            f"{{\\move({width + 20},{y},-{int(estimated_width) + 20},{y})}}{text}"
        )

    header = f"""[Script Info]
; Generated by DouyinLiveRecorder Build 19
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Danmaku,Microsoft YaHei,{font_size},&H00FFFFFF,&H00FFFFFF,&H80000000,&H40000000,0,0,0,0,100,100,0,0,1,1.6,0,7,0,0,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(header + "\n".join(dialogues) + "\n", encoding="utf-8-sig")
    return output_path, len(dialogues)


def _find_ffmpeg() -> str | None:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                executable_dir / "ffmpeg" / "ffmpeg.exe",
                executable_dir / "ffmpeg.exe",
            ]
        )
    module_root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            Path.cwd() / "ffmpeg" / "ffmpeg.exe",
            Path.cwd() / "ffmpeg.exe",
            module_root / "ffmpeg" / "ffmpeg.exe",
            module_root / "ffmpeg.exe",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffmpeg")


def _ffmpeg_filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    value = value.replace(":", r"\:").replace("'", r"\'")
    return value


def _video_candidates(video_path: Path) -> list[Path]:
    if video_path.is_file():
        return [video_path]
    pattern_name = re.sub(r"%0\d+d", "*", video_path.name)
    candidates = [
        path
        for path in video_path.parent.glob(pattern_name)
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if candidates:
        return sorted(candidates)
    prefix = output_prefix(str(video_path))
    candidates = [
        path
        for path in prefix.parent.glob(prefix.name + "*")
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and ".danmaku" not in path.name
    ]
    return sorted(candidates)


def render_danmaku_video(
    video_path: Path,
    ass_path: Path,
    output_path: Path,
    *,
    status_path: Path | None = None,
) -> dict:
    """Render a second MP4 with ASS danmaku, leaving source files untouched."""
    status = {
        "state": "pending",
        "source": str(video_path),
        "output": str(output_path),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    def save_status() -> None:
        if status_path:
            status_path.write_text(
                json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    save_status()
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        status.update(state="skipped", error="未找到 FFmpeg")
        save_status()
        return status

    sources = _video_candidates(video_path)
    if not sources:
        status.update(state="skipped", error="未找到原始录制视频")
        save_status()
        return status

    output_path.parent.mkdir(parents=True, exist_ok=True)
    input_args: list[str]
    if len(sources) == 1:
        input_args = ["-i", str(sources[0])]
    else:
        concat_path = output_path.with_suffix(".concat.txt")
        concat_lines = []
        for source in sources:
            escaped = str(source.resolve()).replace("'", "'\\''")
            concat_lines.append(f"file '{escaped}'")
        concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
        input_args = ["-f", "concat", "-safe", "0", "-i", str(concat_path)]

    filter_value = f"ass='{_ffmpeg_filter_path(ass_path)}'"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        *input_args,
        "-vf",
        filter_value,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    status.update(
        state="rendering",
        ffmpeg=ffmpeg,
        sources=[str(path) for path in sources],
        command=command,
    )
    save_status()
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if completed.returncode == 0 and output_path.is_file():
            status.update(
                state="completed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                size_bytes=output_path.stat().st_size,
            )
        else:
            status.update(
                state="failed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                return_code=completed.returncode,
                error=(completed.stderr or "")[-4000:],
            )
    except Exception as exc:
        status.update(
            state="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        save_status()
    return status


def _keyword_tokens(text: str) -> list[str]:
    text = re.sub(r"https?://\S+", " ", text)
    tokens = re.findall(r"[\u4e00-\u9fff]{2,10}|[A-Za-z][A-Za-z0-9_]{1,24}", text)
    stopwords = {
        "哈哈",
        "哈哈哈",
        "可以",
        "不是",
        "这个",
        "那个",
        "主播",
        "直播",
        "什么",
        "怎么",
        "就是",
        "真的",
        "感觉",
        "一个",
        "我们",
        "你们",
        "他们",
        "666",
    }
    result = []
    for token in tokens:
        normalized = token.lower() if token.isascii() else token
        if normalized in stopwords or len(set(normalized)) == 1:
            continue
        result.append(normalized)
    return result


def summarize_highlights(records: Iterable[dict], highlights: Iterable[dict]) -> list[dict]:
    """Attach an extractive, offline content summary to every highlight range."""
    chats = []
    for record in records:
        if str(record.get("type") or "") not in CHAT_TYPES:
            continue
        text = " ".join(str(record.get("text") or record.get("content") or "").split())
        if not text:
            continue
        try:
            second = max(0, int(float(record.get("second", 0))))
        except (TypeError, ValueError):
            continue
        chats.append((second, text, str(record.get("user_id") or record.get("user") or "")))

    result = []
    for original in highlights:
        item = dict(original)
        start, end = int(item.get("start", 0)), int(item.get("end", 0))
        selected = [(text, user) for second, text, user in chats if start <= second <= end]
        text_counts = Counter(text for text, _ in selected)
        token_counts = Counter(
            token for text, _ in selected for token in _keyword_tokens(text)
        )
        topics = [word for word, _ in token_counts.most_common(5)]
        representatives = [text for text, _ in text_counts.most_common(3)]
        users = {user for _, user in selected if user}
        if topics and representatives:
            summary = (
                f"内容集中在「{'、'.join(topics)}」；代表弹幕："
                + "；".join(f"“{text}”" for text in representatives)
            )
        elif representatives:
            summary = "代表弹幕：" + "；".join(f"“{text}”" for text in representatives)
        else:
            summary = "该区间没有可用于内容总结的文字弹幕。"
        item.update(
            comment_count=len(selected),
            unique_users=len(users),
            topics=topics,
            representative_comments=representatives,
            content_summary=summary,
        )
        result.append(item)
    return result


def _write_report(path: Path, payload: dict) -> None:
    timeline = payload.get("timeline", [])
    highlights = payload.get("highlights", [])
    keywords = payload.get("keywords", [])
    max_heat = max((float(row.get("heat_score", 0)) for row in timeline), default=1.0) or 1.0
    chart_points = []
    if timeline:
        target_points = 900
        step = max(1, math.ceil(len(timeline) / target_points))
        for index in range(0, len(timeline), step):
            window = timeline[index : index + step]
            peak_row = max(window, key=lambda row: float(row.get("heat_score", 0)))
            chart_points.append(
                {
                    "second": int(peak_row.get("second", index)),
                    "heat": float(peak_row.get("heat_score", 0)),
                    "comments": sum(int(row.get("raw_comment_count", 0)) for row in window),
                }
            )

    report_data = {
        "summary": payload.get("summary", {}),
        "highlights": highlights,
        "keywords": keywords,
        "chart": chart_points,
        "maxHeat": max_heat,
        "files": payload.get("files", {}),
        "render": payload.get("render", {}),
    }
    json_blob = json.dumps(report_data, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(str(payload.get("summary", {}).get("title") or "直播弹幕热度报告"))
    document = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light dark;--bg:#0b1020;--card:#141b2d;--muted:#98a2b3;--text:#f5f7fb;--accent:#7c9cff;--hot:#ffb454}
*{box-sizing:border-box}body{margin:0;font-family:"Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:var(--text)}
main{max-width:1280px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;flex-wrap:wrap}
h1{margin:0 0 6px;font-size:28px}.muted{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:20px 0}
.card,.panel{background:var(--card);border:1px solid #26304a;border-radius:14px;padding:16px;box-shadow:0 10px 35px #0003}
.value{font-size:28px;font-weight:700;margin-top:6px}.grid{display:grid;grid-template-columns:2fr 1fr;gap:16px}.panel{margin-bottom:16px}
canvas{width:100%;height:290px;display:block;cursor:crosshair}.chips{display:flex;flex-wrap:wrap;gap:8px}.chip{padding:7px 10px;background:#243052;border-radius:999px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #2a3552}th{color:var(--muted)}
a{color:#9fb4ff}.status{padding:4px 9px;border-radius:999px;background:#243052}.hot{color:var(--hot);font-weight:700}.range-summary{min-width:300px;line-height:1.55}.range-meta{font-size:12px;margin-top:5px}
@media(max-width:850px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body><main>
<div class="top"><div><h1>__TITLE__</h1><div class="muted" id="generated"></div></div><div class="status" id="renderStatus">弹幕视频：读取状态中</div></div>
<section class="cards" id="cards"></section>
<section class="panel"><h2>热度曲线</h2><canvas id="chart" aria-label="可悬停查看秒数的热度曲线"></canvas><div class="muted">鼠标移到曲线上可查看准确秒数、热度和弹幕量；横轴为录制时间。</div></section>
<div class="grid">
<section class="panel"><h2>热点区间与内容总结</h2><table><thead><tr><th>区间</th><th>峰值</th><th>弹幕</th><th>热度</th><th>内容总结</th></tr></thead><tbody id="highlights"></tbody></table></section>
<section class="panel"><h2>关键词</h2><div class="chips" id="keywords"></div></section>
</div>
<section class="panel"><h2>输出文件</h2><div id="files"></div></section>
<script>
const data=__DATA__;
const s=data.summary||{};
const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const clock=value=>{const total=Math.max(0,Math.round(Number(value)||0)),h=Math.floor(total/3600),m=Math.floor(total%3600/60),sec=total%60;return [h,m,sec].map(v=>String(v).padStart(2,'0')).join(':')};
document.getElementById('generated').textContent=`生成时间：${s.generated_at||''} ｜ 时长：${s.duration_text||''}`;
const cards=[
 ['原始弹幕',s.raw_comment_count||0],['有效弹幕',s.filtered_comment_count||0],
 ['参与用户',s.unique_users||0],['礼物事件',s.gift_count||0],
 ['热点区间',(data.highlights||[]).length],['重复率',`${((s.repeat_ratio||0)*100).toFixed(1)}%`]
];
document.getElementById('cards').innerHTML=cards.map(([k,v])=>`<div class="card"><div class="muted">${k}</div><div class="value">${v}</div></div>`).join('');
document.getElementById('highlights').innerHTML=(data.highlights||[]).map((h,i)=>`<tr><td><span class="hot">#${i+1}</span> ${esc(h.start_time)}–${esc(h.end_time)}</td><td>${esc(h.peak_time)}</td><td>${h.total_comments}</td><td>${h.peak_heat_score}</td><td class="range-summary">${esc(h.content_summary||'暂无文字弹幕摘要')}<div class="muted range-meta">${h.comment_count||0} 条文字弹幕 · ${h.unique_users||0} 位用户</div></td></tr>`).join('')||'<tr><td colspan="5" class="muted">暂未检测到明显热点</td></tr>';
document.getElementById('keywords').innerHTML=(data.keywords||[]).slice(0,50).map(k=>`<span class="chip">${esc(k.word)} · ${k.count}</span>`).join('')||'<span class="muted">暂无关键词</span>';
document.getElementById('files').innerHTML=Object.entries(data.files||{}).map(([k,v])=>`<div><b>${esc(k)}</b>：<code>${esc(v)}</code></div>`).join('');
const rs=data.render||{};document.getElementById('renderStatus').textContent=`弹幕视频：${rs.state||'pending'}`;
const canvas=document.getElementById('chart'),ctx=canvas.getContext('2d'),points=data.chart||[];let hoverIndex=-1;
function draw(){const dpr=devicePixelRatio||1,w=canvas.clientWidth,h=canvas.clientHeight;canvas.width=w*dpr;canvas.height=h*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);ctx.strokeStyle='#33405f';ctx.beginPath();for(let i=0;i<5;i++){let y=20+i*(h-40)/4;ctx.moveTo(44,y);ctx.lineTo(w-12,y)}ctx.stroke();if(!points.length)return;ctx.strokeStyle='#7c9cff';ctx.lineWidth=2;ctx.beginPath();points.forEach((p,i)=>{const x=44+i*(w-60)/Math.max(1,points.length-1),y=h-20-(p.heat/(data.maxHeat||1))*(h-40);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke();ctx.fillStyle='#98a2b3';ctx.font='12px sans-serif';ctx.fillText('0',18,h-18);ctx.fillText(String(Math.round(data.maxHeat||0)),8,24);ctx.fillText(clock(points[0].second),44,h-4);const endLabel=clock(points[points.length-1].second),endWidth=ctx.measureText(endLabel).width;ctx.fillText(endLabel,w-12-endWidth,h-4);if(hoverIndex<0)return;const p=points[hoverIndex],x=44+hoverIndex*(w-60)/Math.max(1,points.length-1),y=h-20-(p.heat/(data.maxHeat||1))*(h-40);ctx.strokeStyle='#ffb454';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,20);ctx.lineTo(x,h-20);ctx.stroke();ctx.fillStyle='#ffb454';ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();const label=`${clock(p.second)}（${p.second}秒）  热度 ${Number(p.heat).toFixed(1)}  弹幕 ${p.comments}`,pad=8,boxW=ctx.measureText(label).width+pad*2,boxX=Math.min(Math.max(4,x-boxW/2),w-boxW-4);ctx.fillStyle='#080d19eF';ctx.fillRect(boxX,4,boxW,28);ctx.fillStyle='#fff';ctx.fillText(label,boxX+pad,22)}
canvas.addEventListener('mousemove',event=>{const rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left;hoverIndex=Math.max(0,Math.min(points.length-1,Math.round((x-44)/Math.max(1,rect.width-60)*Math.max(1,points.length-1))));draw()});canvas.addEventListener('mouseleave',()=>{hoverIndex=-1;draw()});addEventListener('resize',draw);draw();
</script></main></body></html>"""
    path.write_text(
        document.replace("__TITLE__", title).replace("__DATA__", json_blob),
        encoding="utf-8",
    )


class DanmakuSession:
    """Run a collector and write complete recording sidecars."""

    def __init__(
        self,
        video_path: str,
        room_url: str,
        cookie: str,
        command: str = "",
        highlight_options: dict | None = None,
        relay_executable: str = "",
        relay_port: int = 1088,
        duplicate_window: int = 60,
    ):
        self.video_path = Path(video_path)
        self.prefix = output_prefix(video_path)
        self.room_url = room_url
        self.cookie = cookie
        self.command = command
        self.relay_executable = relay_executable
        self.relay_port = relay_port
        self.duplicate_window = max(0, duplicate_window)
        self.highlight_options = highlight_options or {}
        self.started_at = time.monotonic()
        self.started_wall_time = time.time()
        self.stop_event = threading.Event()
        self.events: queue.Queue[tuple[int, dict]] = queue.Queue()
        self.rows: list[SecondStats] = []
        self.users: dict[int, set[str]] = {}
        self.all_users: set[str] = set()
        self.process: subprocess.Popen | None = None
        self.websocket = None
        self.connected = False
        self.raw_event_count = 0
        self.system_status: dict = {}
        self.last_error = ""
        self.seen_content: dict[str, int] = {}
        self.unique_chat_count = 0
        self.duplicate_chat_count = 0
        self.keyword_counts: Counter[str] = Counter()
        self.keyword_seconds: dict[str, Counter[int]] = defaultdict(Counter)
        self.sequence = 0
        self.message_path = self.prefix.with_suffix(".danmaku.txt")
        self.raw_path = self.prefix.with_suffix(".danmaku.raw.jsonl")
        self.message_file = None
        self.raw_file = None
        self.reader_thread: threading.Thread | None = None
        self.writer_thread: threading.Thread | None = None
        self.render_thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()
        self._stop_result: tuple[Path, Path] | None = None

    @property
    def platform(self) -> str:
        return "douyu" if "douyu.com/" in self.room_url else "douyin"

    def _open_outputs(self) -> None:
        self.message_path.parent.mkdir(parents=True, exist_ok=True)
        self.message_file = self.message_path.open(
            "w", encoding="utf-8-sig", buffering=1
        )
        self.raw_file = self.raw_path.open("w", encoding="utf-8", buffering=1)

    def start(self) -> None:
        self._open_outputs()
        if not self.command:
            ensure_douyin_relay(self.relay_executable, self.relay_port)
            self.reader_thread = threading.Thread(
                target=self._read_relay_events, daemon=True
            )
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
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
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
                    self._relay_url(), timeout=10, enable_multithread=True
                )
                self.connected = True
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
                    if isinstance(event, dict):
                        if event.get("type") == "system":
                            self.system_status = event
                        else:
                            self.raw_event_count += 1
                            second = max(0, int(time.monotonic() - self.started_at))
                            self.events.put((second, event))
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
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
                    self.raw_event_count += 1
                    second = max(0, int(time.monotonic() - self.started_at))
                    self.events.put((second, event))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

    def _ensure_row(self, second: int) -> SecondStats:
        while len(self.rows) <= second:
            self.rows.append(SecondStats(second=len(self.rows)))
        return self.rows[second]

    def _write_raw(self, second: int, event: dict) -> dict:
        self.sequence += 1
        user_id, nickname = _event_user(event)
        event_type = _event_type(event)
        content = _event_content(event)
        record = {
            "sequence": self.sequence,
            "second": second,
            "time": _clock(second),
            "captured_at": datetime.fromtimestamp(
                self.started_wall_time + second, tz=timezone.utc
            ).isoformat(),
            "platform": self.platform,
            "type": event_type,
            "user_id": user_id,
            "user": nickname,
            "text": content,
            "count": event.get("count", event.get("repeatCount", 1)),
            "raw": _safe_json(event),
        }
        if self.raw_file:
            self.raw_file.write(
                json.dumps(record, ensure_ascii=False, default=str) + "\n"
            )
        return record

    def _consume(self, second: int, event: dict) -> None:
        row = self._ensure_row(second)
        record = self._write_raw(second, event)
        event_type = str(record["type"])
        if event_type in CHAT_TYPES:
            content = str(record["text"])
            if not content:
                return
            row.raw_comment_count += 1
            user_id = str(record["user_id"] or record["user"] or "")
            if user_id:
                self.all_users.add(user_id)
                users = self.users.setdefault(second, set())
                users.add(user_id)
                row.unique_users = len(users)

            for token in _keyword_tokens(content):
                self.keyword_counts[token] += 1
                self.keyword_seconds[token][second] += 1

            previous_second = self.seen_content.get(content)
            if (
                previous_second is not None
                and self.duplicate_window > 0
                and second - previous_second <= self.duplicate_window
            ):
                self.duplicate_chat_count += 1
                row.duplicate_count += 1
                return
            self.seen_content[content] = second
            self.unique_chat_count += 1
            row.comment_count += 1
            if self.message_file:
                self.message_file.write(f"[{_clock(second)}] {content}\n")
        elif event_type in GIFT_TYPES:
            try:
                row.gift_count += max(
                    1, int(event.get("count", event.get("repeatCount", 1)) or 1)
                )
            except (TypeError, ValueError):
                row.gift_count += 1
        elif event_type in LIKE_TYPES:
            try:
                row.like_count += max(1, int(event.get("count", 1) or 1))
            except (TypeError, ValueError):
                row.like_count += 1

    def _aggregate(self) -> None:
        while not self.stop_event.is_set() or not self.events.empty():
            try:
                second, event = self.events.get(timeout=0.2)
                self._consume(second, event)
            except queue.Empty:
                elapsed = max(0, int(time.monotonic() - self.started_at))
                self._ensure_row(elapsed)

    def _read_raw_records(self) -> list[dict]:
        records: list[dict] = []
        if not self.raw_path.is_file():
            return records
        with self.raw_path.open("r", encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        records.append(value)
                except json.JSONDecodeError:
                    continue
        return records

    def _keyword_payload(self) -> list[dict]:
        items = []
        for word, count in self.keyword_counts.most_common(100):
            seconds = self.keyword_seconds.get(word, Counter())
            peak_second = max(seconds, key=seconds.get) if seconds else 0
            first_second = min(seconds) if seconds else 0
            items.append(
                {
                    "word": word,
                    "count": count,
                    "first_second": first_second,
                    "first_time": _clock(first_second),
                    "peak_second": peak_second,
                    "peak_time": _clock(peak_second),
                    "peak_count": seconds.get(peak_second, 0),
                }
            )
        return items

    def _start_render(self, report_payload: dict, report_path: Path) -> None:
        ass_path = Path(report_payload["files"]["ass"])
        output_path = Path(report_payload["files"]["danmaku_video"])
        status_path = Path(report_payload["files"]["render_status"])
        if not _video_candidates(self.video_path):
            status = {
                "state": "skipped",
                "source": str(self.video_path),
                "output": str(output_path),
                "error": "未找到原始录制视频",
            }
            status_path.write_text(
                json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            report_payload["render"] = status
            _write_report(report_path, report_payload)
            return

        def worker() -> None:
            status = render_danmaku_video(
                self.video_path, ass_path, output_path, status_path=status_path
            )
            report_payload["render"] = status
            _write_report(report_path, report_payload)

        self.render_thread = threading.Thread(
            target=worker,
            name=f"danmaku_render_{self.prefix.name}",
            daemon=False,
        )
        self.render_thread.start()

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
                self.reader_thread.join(timeout=3)
            if self.writer_thread:
                self.writer_thread.join(timeout=5)
            if self.message_file:
                self.message_file.flush()
                self.message_file.close()
                self.message_file = None
            if self.raw_file:
                self.raw_file.flush()
                self.raw_file.close()
                self.raw_file = None

            self._ensure_row(max(0, int(time.monotonic() - self.started_at)))
            _compute_heat(self.rows)

            legacy_csv_path = self.prefix.with_suffix(".danmaku.csv")
            timeline_path = self.prefix.with_suffix(".danmaku.timeline.csv")
            highlight_path = self.prefix.with_suffix(".highlights.json")
            keywords_path = self.prefix.with_suffix(".danmaku.keywords.json")
            report_path = self.prefix.with_suffix(".danmaku.report.html")
            ass_path = self.prefix.with_suffix(".danmaku.ass")
            danmaku_video_path = self.prefix.with_suffix(".danmaku.mp4")
            render_status_path = self.prefix.with_suffix(".danmaku.render.json")

            timeline_path.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = list(asdict(SecondStats(0)).keys())
            for csv_path in (legacy_csv_path, timeline_path):
                with csv_path.open(
                    "w", encoding="utf-8-sig", newline=""
                ) as file:
                    writer = csv.DictWriter(file, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(asdict(row) for row in self.rows)

            highlights = detect_highlights(self.rows, **self.highlight_options)
            keywords = self._keyword_payload()
            keywords_path.write_text(
                json.dumps(keywords, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            raw_records = self._read_raw_records()
            highlights = summarize_highlights(raw_records, highlights)
            _, displayed_count = build_ass(raw_records, ass_path)

            total_raw = sum(row.raw_comment_count for row in self.rows)
            total_filtered = sum(row.comment_count for row in self.rows)
            total_duplicate = sum(row.duplicate_count for row in self.rows)
            total_gifts = sum(row.gift_count for row in self.rows)
            total_likes = sum(row.like_count for row in self.rows)
            summary = {
                "title": f"{self.platform.upper()} 直播弹幕热度报告",
                "platform": self.platform,
                "room_url": self.room_url,
                "generated_at": datetime.now().astimezone().isoformat(),
                "duration_seconds": len(self.rows),
                "duration_text": _clock(len(self.rows)),
                "raw_event_count": self.raw_event_count,
                "raw_comment_count": total_raw,
                "filtered_comment_count": total_filtered,
                "duplicate_comment_count": total_duplicate,
                "repeat_ratio": round(total_duplicate / total_raw, 4)
                if total_raw
                else 0.0,
                "unique_users": len(self.all_users),
                "gift_count": total_gifts,
                "like_count": total_likes,
                "displayed_danmaku_count": displayed_count,
                "collector_connected": self.connected,
                "collector_last_error": self.last_error,
            }
            files = {
                "original_video": str(self.video_path),
                "danmaku_video": str(danmaku_video_path),
                "raw_jsonl": str(self.raw_path),
                "message_text": str(self.message_path),
                "timeline_csv": str(timeline_path),
                "legacy_csv": str(legacy_csv_path),
                "keywords_json": str(keywords_path),
                "highlights_json": str(highlight_path),
                "report_html": str(report_path),
                "ass": str(ass_path),
                "render_status": str(render_status_path),
            }
            payload = {
                "video": str(self.prefix),
                "summary": summary,
                "collector": {
                    "connected": self.connected,
                    "raw_event_count": self.raw_event_count,
                    "unique_chat_count": self.unique_chat_count,
                    "duplicate_chat_count": self.duplicate_chat_count,
                    "duplicate_window_seconds": self.duplicate_window,
                    "message_file": str(self.message_path),
                    "raw_file": str(self.raw_path),
                    "last_system_status": self.system_status,
                    "last_error": self.last_error,
                },
                "timeline": [asdict(row) for row in self.rows],
                "keywords": keywords,
                "highlights": highlights,
                "files": files,
                "render": {"state": "pending"},
            }
            highlight_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _write_report(report_path, payload)
            render_status_path.write_text(
                json.dumps(
                    {
                        "state": "pending",
                        "output": str(danmaku_video_path),
                        "message": "录制结束后正在生成弹幕版视频",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            self._stop_result = (timeline_path, highlight_path)
            with _sessions_lock:
                _active_sessions.discard(self)
            self._start_render(payload, report_path)
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
