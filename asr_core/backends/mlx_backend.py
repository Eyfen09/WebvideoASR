from __future__ import annotations

from pathlib import Path


class MLXSession:
    """Thin wrapper around mlx_qwen3_asr.Session."""

    def __init__(self, model_path: Path, *, forced_aligner: str | None = None) -> None:
        from mlx_qwen3_asr import Session

        self._session = Session(model=str(model_path))
        _ = forced_aligner  # unused – passed per-call in transcribe()

    def transcribe(self, audio: str, **kwargs: object) -> object:
        return self._session.transcribe(audio, **kwargs)
