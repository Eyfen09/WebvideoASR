from __future__ import annotations

from pathlib import Path


class TorchSession:
    """Thin wrapper around qwen_asr.Qwen3ASRModel."""

    def __init__(
        self,
        model_path: Path,
        *,
        device: str = "cuda",
        forced_aligner: str | None = None,
    ) -> None:
        import torch
        from qwen_asr import Qwen3ASRModel

        device_map = "cuda:0" if device == "cuda" else "cpu"
        dtype = torch.bfloat16 if device == "cuda" else torch.float32

        init_kwargs: dict = {"dtype": dtype, "device_map": device_map}
        if forced_aligner:
            init_kwargs["forced_aligner"] = forced_aligner
            init_kwargs["forced_aligner_kwargs"] = {"dtype": dtype, "device_map": device_map}

        self._model = Qwen3ASRModel.from_pretrained(str(model_path.resolve()), **init_kwargs)

    def transcribe(self, audio: str, **kwargs: object) -> object:
        language = kwargs.get("language")
        return_timestamps = kwargs.get("return_timestamps", False)

        qwen_kwargs: dict = {}
        if language is not None:
            qwen_kwargs["language"] = language
        if return_timestamps:
            qwen_kwargs["return_time_stamps"] = True

        results = self._model.transcribe(str(audio), **qwen_kwargs)
        result = results[0] if results else None

        # Normalise: qwen_asr uses .time_stamps, MLX/our pipeline expects .segments
        if result is not None and hasattr(result, "time_stamps") and not hasattr(result, "segments"):
            result.segments = result.time_stamps  # type: ignore[attr-defined]

        return result
