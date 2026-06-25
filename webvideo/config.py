from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent


@dataclass(frozen=True)
class WebVideoConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True
    model_path: Path = PROJECT_ROOT / "model" / "Qwen3-ASR-1.7B"
    device: str = "gpu"
    language: str | None = None
    timestamps: bool = False
    forced_aligner: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    output_dir: Path = PROJECT_ROOT / "output" / "webvideo"
    cache_dir: Path = PROJECT_ROOT / ".cache" / "webvideo"
    database_path: Path = PROJECT_ROOT / "data" / "webvideo.sqlite3"
    browser_profile_dir: Path = PROJECT_ROOT / "data" / "webvideo-browser"
    cookie_file: Path = PROJECT_ROOT / "data" / "webvideo-cookies.txt"
    browser_executable: Path = Path(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    preferred_login_mode: Literal["qrcode", "browser"] = "qrcode"
    use_scrapling_backend: bool = True
    auto_import_xiaohongshu_chrome: bool = True
    auto_import_youtube_chrome: bool = True
    fallback_jaccard_threshold: float = 0.72
    qrcode_poll_interval_seconds: float = 1.0
    request_timeout_seconds: float = 45.0
    scroll_wait_seconds: float = 1.0
    stable_scroll_rounds: int = 3
    max_scroll_rounds: int = 500
    probe_concurrency: int = 4
    download_concurrency: int = 2
    download_retries: int = 3
    page_size: int = 100

    def prepare_directories(self) -> None:
        for path in (
            self.output_dir,
            self.cache_dir,
            self.database_path.parent,
            self.browser_profile_dir,
            self.cookie_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("通用视频服务只能监听本机地址")
        if not 1 <= self.port <= 65535:
            raise ValueError("端口必须在 1 到 65535 之间")
        if self.download_concurrency < 1 or self.probe_concurrency < 1:
            raise ValueError("并发数必须大于零")
        if self.preferred_login_mode not in {"qrcode", "browser"}:
            raise ValueError("preferred_login_mode 必须是 qrcode 或 browser")
        if not 0 <= self.fallback_jaccard_threshold <= 1:
            raise ValueError("fallback_jaccard_threshold 必须在 0 到 1 之间")


DEFAULT_CONFIG = WebVideoConfig()
