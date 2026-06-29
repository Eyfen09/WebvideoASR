from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .audio import ffmpeg_executable


DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start: float
    end: float


def format_hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remaining = divmod(total, 3600)
    minutes, secs = divmod(remaining, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def media_duration_seconds(audio_path: Path) -> float:
    ffmpeg_path = ffmpeg_executable()
    if ffmpeg_path is None:
        raise RuntimeError("切段转写需要 FFmpeg")
    command = [ffmpeg_path, "-hide_banner", "-i", str(audio_path)]
    completed = subprocess.run(command, capture_output=True, text=True)
    output = f"{completed.stderr}\n{completed.stdout}"
    match = DURATION_RE.search(output)
    if match is None:
        raise RuntimeError(f"无法读取媒体时长：{audio_path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def chunk_ranges(
    duration: float, chunk_seconds: int, overlap_seconds: int
) -> list[tuple[float, float]]:
    if chunk_seconds <= 0:
        return [(0.0, max(0.0, duration))]
    if overlap_seconds < 0 or overlap_seconds >= chunk_seconds:
        raise ValueError("chunk_overlap_seconds 必须小于 chunk_seconds")
    end_limit = max(0.0, float(duration))
    if end_limit <= chunk_seconds:
        return [(0.0, end_limit)]
    ranges: list[tuple[float, float]] = []
    base_start = 0.0
    while base_start < end_limit:
        start = max(0.0, base_start - overlap_seconds)
        end = min(base_start + chunk_seconds, end_limit)
        ranges.append((start, end))
        if end >= end_limit:
            break
        base_start += chunk_seconds
    return ranges


@contextmanager
def split_audio_chunks(
    audio_path: Path, chunk_seconds: int, overlap_seconds: int
) -> Iterator[list[AudioChunk]]:
    duration = media_duration_seconds(audio_path)
    ranges = chunk_ranges(duration, chunk_seconds, overlap_seconds)
    ffmpeg_path = ffmpeg_executable()
    if ffmpeg_path is None:
        raise RuntimeError("切段转写需要 FFmpeg")
    with tempfile.TemporaryDirectory(prefix="videoasr-chunks-") as temp_dir:
        root = Path(temp_dir)
        chunks: list[AudioChunk] = []
        for index, (start, end) in enumerate(ranges, start=1):
            destination = root / f"chunk-{index:04d}.wav"
            command = [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(audio_path),
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{max(0.001, end - start):.3f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                str(destination),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "").strip()
                if detail:
                    raise RuntimeError(f"音频切段失败：{detail}") from exc
                raise RuntimeError(f"音频切段失败：{audio_path}") from exc
            chunks.append(AudioChunk(destination, start, end))
        yield chunks
