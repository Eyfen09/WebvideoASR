from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """A user-facing configuration error."""


class Device(Enum):
    GPU = "gpu"
    CPU = "cpu"


@dataclass(frozen=True)
class ASRConfig:
    model_path: Path
    device: Device
    language: str | None
    timestamps: bool
    forced_aligner: str
    output_dir: Path
    chunk_seconds: int = 0
    chunk_overlap_seconds: int = 0


def require_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ConfigError(f"配置缺少对象：{key}")
    return value


def require_string(data: Mapping[str, Any], path: str, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"配置项 {path}.{key} 必须是非空字符串")
    return value.strip()


def optional_non_negative_int(
    data: Mapping[str, Any], path: str, key: str, default: int
) -> int:
    value = data.get(key, default)
    if isinstance(value, bool):
        raise ConfigError(f"配置项 {path}.{key} 必须是非负整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {path}.{key} 必须是非负整数") from exc
    if number < 0:
        raise ConfigError(f"配置项 {path}.{key} 必须是非负整数")
    return number


def resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def load_yaml(config_path: Path) -> Mapping[str, Any]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            raw = yaml.safe_load(config_file)
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到配置文件：{config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件格式错误：{exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError("配置文件顶层必须是对象")
    return raw


def load_asr_config(config_path: Path) -> ASRConfig:
    """Read and validate a local ASR model configuration file."""
    raw = load_yaml(config_path)
    model = require_mapping(raw, "model")
    transcription = require_mapping(raw, "transcription")
    output = require_mapping(raw, "output")

    model_path = resolve_path(
        require_string(model, "model", "path"), config_path.parent
    )
    if not model_path.is_dir():
        raise ConfigError(f"模型目录不存在：{model_path}")

    language_value = transcription.get("language")
    if language_value is None:
        language = None
    elif isinstance(language_value, str) and language_value.strip():
        language = language_value.strip()
    else:
        raise ConfigError("配置项 transcription.language 必须是非空字符串或 null")

    device_raw = model.get("device", "gpu")
    if device_raw not in ("gpu", "cpu"):
        raise ConfigError("配置项 model.device 必须是 gpu 或 cpu")
    device = Device(device_raw)

    timestamps = transcription.get("timestamps")
    if not isinstance(timestamps, bool):
        raise ConfigError("配置项 transcription.timestamps 必须是 true 或 false")

    forced_aligner = require_string(
        transcription, "transcription", "forced_aligner"
    )
    chunk_seconds = optional_non_negative_int(
        transcription, "transcription", "chunk_seconds", 300
    )
    chunk_overlap_seconds = optional_non_negative_int(
        transcription, "transcription", "chunk_overlap_seconds", 10
    )
    if chunk_seconds > 0 and chunk_overlap_seconds >= chunk_seconds:
        raise ConfigError(
            "配置项 transcription.chunk_overlap_seconds 必须小于 chunk_seconds"
        )
    output_dir = resolve_path(
        require_string(output, "output", "directory"), config_path.parent
    )
    return ASRConfig(
        model_path=model_path,
        device=device,
        language=language,
        timestamps=timestamps,
        forced_aligner=forced_aligner,
        output_dir=output_dir,
        chunk_seconds=chunk_seconds,
        chunk_overlap_seconds=chunk_overlap_seconds,
    )
