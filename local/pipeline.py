from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from asr_core.config import ASRConfig
from asr_core.engine import make_progress_callback, transcribe_audio
from asr_core.formatting import format_transcript, write_text_atomic


SUPPORTED_EXTENSIONS = frozenset(
    {
        ".aac", ".flac", ".m4a", ".mkv", ".mov", ".mp3",
        ".mp4", ".ogg", ".opus", ".wav", ".webm", ".wma",
    }
)


def describe_exception(exc: Exception) -> str:
    message = str(exc).strip()
    return message or type(exc).__name__


def discover_audio_files(input_path: Path) -> list[Path]:
    path = input_path.expanduser()
    if not path.exists():
        raise ValueError(f"输入路径不存在：{path}")
    if path.is_file():
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的音频格式：{path.suffix or '(无扩展名)'}")
        return [path]
    if not path.is_dir():
        raise ValueError(f"输入路径不是普通文件或目录：{path}")
    files = [
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    return sorted(files, key=lambda candidate: candidate.as_posix().casefold())


def output_path_for(audio_path: Path, input_path: Path, output_dir: Path) -> Path:
    if input_path.is_dir():
        return output_dir / audio_path.relative_to(input_path).with_suffix(".txt")
    return output_dir / f"{audio_path.stem}.txt"


def process_files(
    audio_files: Sequence[Path],
    *,
    input_path: Path,
    config: ASRConfig,
    session: object,
) -> tuple[int, int]:
    successes = 0
    failures = 0
    for index, audio_path in enumerate(audio_files, start=1):
        print(f"[{index}/{len(audio_files)}] 转写：{audio_path}")
        try:
            result = transcribe_audio(
                session,
                audio_path,
                config,
                on_progress=make_progress_callback(),
            )
            content = format_transcript(result, timestamps=config.timestamps)
            destination = output_path_for(audio_path, input_path, config.output_dir)
            write_text_atomic(destination, content)
        except Exception as exc:
            failures += 1
            print(f"    失败：{describe_exception(exc)}", file=sys.stderr)
            continue
        successes += 1
        print(f"    已保存：{destination}")
    return successes, failures
