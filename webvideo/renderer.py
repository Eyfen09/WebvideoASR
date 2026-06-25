from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from asr_core.formatting import write_text_atomic

from .models import format_duration


def safe_title(title: str, *, max_length: int = 72) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", str(title or ""))
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return (text[:max_length].rstrip() or "untitled")


def short_identity(record: dict[str, Any]) -> str:
    media_id = re.sub(r"[^0-9A-Za-z_-]+", "", str(record.get("media_id") or ""))
    if media_id:
        return media_id[:32]
    return hashlib.sha256(str(record["identity"]).encode()).hexdigest()[:12]


def output_path_for(record: dict[str, Any], output_dir: Path) -> Path:
    return output_dir / f"{safe_title(record.get('title', ''))}_{short_identity(record)}.txt"


def render_content(record: dict[str, Any]) -> str:
    return (
        f"视频标题：{record.get('title', '')}\n"
        f"媒体 ID：{record.get('media_id', '')}\n"
        f"原始链接：{record.get('webpage_url') or record.get('url', '')}\n"
        f"来源页面：{record.get('source_page', '')}\n"
        f"作者：{record.get('author', '')}\n"
        f"视频时长：{format_duration(record.get('duration_seconds'))}\n"
        f"检测语言：{record.get('detected_language', '')}\n"
        f"转录时间：{record.get('completed_at') or record.get('updated_at', '')}\n"
        "\n完整转录：\n"
        f"{str(record.get('transcript_text') or '').rstrip()}\n"
    )


def render_record(record: dict[str, Any], output_dir: Path) -> Path:
    destination = output_path_for(record, output_dir)
    write_text_atomic(destination, render_content(record))
    old_value = str(record.get("output_path") or "")
    if old_value:
        old_path = Path(old_value)
        if old_path != destination and old_path.is_file():
            old_path.unlink()
    return destination
