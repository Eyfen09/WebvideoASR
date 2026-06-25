from __future__ import annotations

import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any


SENTENCE_END_RE = re.compile(r"[。！？!?]$")
CJK_LANGUAGE_ALIASES = frozenset(
    {
        "cantonese",
        "chinese",
        "ja",
        "japanese",
        "jp",
        "ko",
        "korean",
        "kr",
        "yue",
        "zh",
        "zh-cn",
        "zh-tw",
    }
)


def result_to_dict(result: object) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    return {
        "text": getattr(result, "text", ""),
        "language": getattr(result, "language", None),
        "segments": getattr(result, "segments", None),
        "chunks": getattr(result, "chunks", None),
    }


def format_plain_text(result: dict[str, Any]) -> str:
    text = str(result.get("text") or "").rstrip()
    return f"{text}\n"


def _format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remaining = divmod(total_ms, 3_600_000)
    minutes, remaining = divmod(remaining, 60_000)
    secs, millis = divmod(remaining, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _normalized_segments(raw_segments: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for raw in raw_segments:
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(raw.get("start", 0.0))
            end = max(start, float(raw.get("end", start)))
        except (TypeError, ValueError) as exc:
            raise ValueError("转写结果包含无效时间戳") from exc
        segments.append({"text": text, "start": start, "end": end})
    return segments


def _restore_punctuation(
    transcript: str, segments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not transcript.strip() or not segments:
        return segments
    folded = transcript.casefold()
    positions: list[tuple[int, int]] = []
    cursor = 0
    for segment in segments:
        token = str(segment["text"])
        index = folded.find(token.casefold(), cursor)
        if index < 0:
            return segments
        end = index + len(token)
        positions.append((index, end))
        cursor = end

    restored: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        text_start = 0 if index == 0 else positions[index][0]
        text_end = (
            positions[index + 1][0]
            if index + 1 < len(positions)
            else len(transcript)
        )
        text = transcript[text_start:text_end].strip()
        restored.append({**segment, "text": text or segment["text"]})
    return restored


def _join_text(segments: Sequence[dict[str, Any]], language: str) -> str:
    parts = [str(segment["text"]).strip() for segment in segments]
    if language.strip().lower() in CJK_LANGUAGE_ALIASES:
        return "".join(parts)
    text = " ".join(parts)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"([({\[])\s+", r"\1", text).strip()


def group_timestamp_segments(
    raw_segments: Sequence[dict[str, Any]],
    *,
    transcript: str = "",
    language: str = "Chinese",
    max_chars: int = 80,
    max_duration: float = 15.0,
    max_gap: float = 1.0,
) -> list[dict[str, Any]]:
    segments = _restore_punctuation(transcript, _normalized_segments(raw_segments))
    grouped: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if current:
            grouped.append(
                {
                    "text": _join_text(current, language),
                    "start": float(current[0]["start"]),
                    "end": float(current[-1]["end"]),
                }
            )
            current.clear()

    for segment in segments:
        if current:
            gap = float(segment["start"]) - float(current[-1]["end"])
            candidate = _join_text([*current, segment], language)
            duration = float(segment["end"]) - float(current[0]["start"])
            if gap >= max_gap or len(candidate) > max_chars or duration > max_duration:
                flush()
        current.append(segment)
        if SENTENCE_END_RE.search(str(segment["text"]).strip()):
            flush()
    flush()
    return grouped


def format_timestamp_text(result: dict[str, Any]) -> str:
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("已请求时间戳，但模型没有返回 segments")
    grouped = group_timestamp_segments(
        raw_segments,
        transcript=str(result.get("text") or ""),
        language=str(result.get("language") or "Chinese"),
    )
    if not grouped:
        raise ValueError("模型返回的时间戳内容为空")
    return "\n".join(
        f"[{_format_timestamp(item['start'])} - {_format_timestamp(item['end'])}] "
        f"{item['text']}"
        for item in grouped
    ) + "\n"


def format_transcript(result: dict[str, Any], *, timestamps: bool) -> str:
    return format_timestamp_text(result) if timestamps else format_plain_text(result)


def write_text_atomic(output_path: Path, content: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(content)
        Path(temp_name).replace(output_path)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
