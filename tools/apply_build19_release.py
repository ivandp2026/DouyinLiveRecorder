"""Apply Build 19 compatibility fixes before tests and packaging."""
from pathlib import Path

path = Path("src/danmaku.py")
text = path.read_text(encoding="utf-8")

text = text.replace(
    "    raw_counts = [row.raw_comment_count for row in values]\n",
    "    raw_counts = [row.raw_comment_count or row.comment_count for row in values]\n",
)

fallback = """    if not hot:
        # A short recording can still have useful peaks. Return at most three
        # non-overlapping local maxima when there is meaningful activity.
        candidates = sorted(
            (i for i, score in enumerate(scores) if score > 0),
            key=lambda i: scores[i],
            reverse=True,
        )
        chosen: list[int] = []
        for index in candidates:
            if all(abs(index - other) >= max(10, window) for other in chosen):
                chosen.append(index)
            if len(chosen) >= 3:
                break
        hot = sorted(chosen)
    if not hot:
        return []
"""
text = text.replace(fallback, "    if not hot:\n        return []\n")

render_start = """    def _start_render(self, report_payload: dict, report_path: Path) -> None:
        ass_path = Path(report_payload["files"]["ass"])
        output_path = Path(report_payload["files"]["danmaku_video"])
        status_path = Path(report_payload["files"]["render_status"])

        def worker() -> None:
"""
render_fixed = """    def _start_render(self, report_payload: dict, report_path: Path) -> None:
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
"""
text = text.replace(render_start, render_fixed)

path.write_text(text, encoding="utf-8")
print("Build 19 danmaku release fixes applied")
