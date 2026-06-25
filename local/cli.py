from __future__ import annotations

import sys
from pathlib import Path

from asr_core.config import ConfigError, Device, load_asr_config
from asr_core.engine import create_session

from .pipeline import discover_audio_files, process_files


CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def run(input_path: Path, *, cpu: bool = False) -> int:
    try:
        config = load_asr_config(CONFIG_PATH)
        device = Device.CPU if cpu else config.device
        source = input_path.expanduser()
        audio_files = discover_audio_files(source)
    except (ConfigError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if not audio_files:
        print(f"错误：目录中没有支持的音频文件：{source}", file=sys.stderr)
        return 2

    print(f"正在加载模型：{config.model_path}")
    try:
        session = create_session(
            config.model_path, device, forced_aligner=config.forced_aligner
        )
    except Exception as exc:
        print(f"错误：模型加载失败：{exc}", file=sys.stderr)
        return 2
    print("模型加载完成。")
    successes, failures = process_files(
        audio_files, input_path=source, config=config, session=session
    )
    print(f"完成：成功 {successes} 个，失败 {failures} 个。")
    return 0 if failures == 0 else 1
