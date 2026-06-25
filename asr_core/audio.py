from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def is_riff_wave(audio_path: Path) -> bool:
    try:
        header = audio_path.read_bytes()[:12]
    except OSError:
        return False
    return header[:4] == b"RIFF" and header[8:12] == b"WAVE"


def needs_windows_wav_conversion(audio_path: Path) -> bool:
    return sys.platform == "win32" and not is_riff_wave(audio_path)


def ffmpeg_executable() -> str | None:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is not None:
        return ffmpeg_path
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
    except ImportError:
        return None
    return get_ffmpeg_exe()


@contextmanager
def prepared_audio_for_model(audio_path: Path) -> Iterator[Path]:
    path = audio_path.expanduser()
    if not needs_windows_wav_conversion(path):
        yield path
        return

    ffmpeg_path = ffmpeg_executable()
    if ffmpeg_path is None:
        raise RuntimeError(
            "Windows 转写 m4a/mp3/mp4 等格式需要 FFmpeg；"
            "请运行 uv sync 安装项目依赖，或安装系统 FFmpeg 并确保 ffmpeg.exe 在 PATH 中"
        )

    with tempfile.TemporaryDirectory(prefix="videoasr-audio-") as temp_dir:
        converted = Path(temp_dir) / f"{path.stem}.wav"
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            str(converted),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            if detail:
                raise RuntimeError(f"音频转码失败：{detail}") from exc
            raise RuntimeError(f"音频转码失败：{path}") from exc
        yield converted
