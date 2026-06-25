from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .audio import prepared_audio_for_model
from .backends.mlx_backend import MLXSession
from .backends.torch_backend import TorchSession
from .config import ASRConfig, Device
from .formatting import result_to_dict


def create_session(
    model_path: Path,
    device: Device = Device.GPU,
    *,
    forced_aligner: str | None = None,
) -> object:
    if device == Device.CPU:
        print(f"Device: CPU（PyTorch）")
        return TorchSession(model_path, device="cpu", forced_aligner=forced_aligner)
    if sys.platform == "darwin":
        print(f"Device: GPU（MLX）")
        return MLXSession(model_path, forced_aligner=forced_aligner)
    print(f"Device: GPU（CUDA）")
    return TorchSession(model_path, device="cuda", forced_aligner=forced_aligner)


def make_progress_callback(prefix: str = "    ") -> Callable[[dict[str, Any]], None]:
    """Create a concise progress reporter for long, chunked audio."""

    def on_progress(event: dict[str, Any]) -> None:
        event_name = event.get("event")
        if event_name == "chunks_prepared":
            duration = float(event.get("audio_duration_sec", 0.0))
            total = int(event.get("total_chunks", 0))
            print(f"{prefix}音频时长：{duration:.1f} 秒，分块：{total}")
        elif event_name == "chunk_started":
            index = int(event.get("chunk_index", 0))
            total = int(event.get("total_chunks", 0))
            print(f"{prefix}正在转写分块 {index}/{total}…")

    return on_progress


def transcribe_audio(
    session: object,
    audio_path: Path,
    config: ASRConfig,
    *,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Transcribe one audio file without crossing a thread boundary."""
    options: dict[str, Any] = {
        "return_timestamps": config.timestamps,
        "return_chunks": True,
        "on_progress": on_progress,
    }
    if config.language is not None:
        options["language"] = config.language
    if config.timestamps:
        options["forced_aligner"] = config.forced_aligner

    transcribe = getattr(session, "transcribe")
    with prepared_audio_for_model(audio_path) as model_audio_path:
        return result_to_dict(transcribe(str(model_audio_path), **options))
