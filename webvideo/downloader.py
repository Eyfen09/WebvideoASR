from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from yt_dlp.networking.impersonate import ImpersonateTarget

from .config import WebVideoConfig
from .ytdlp_runtime import JsRuntimeOptions, detect_js_runtimes


MEDIA_EXTENSIONS = frozenset(
    {
        ".aac",
        ".flac",
        ".flv",
        ".m4a",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
    }
)


class MediaDownloadError(RuntimeError):
    def __init__(self, message: str, *, unsupported: bool = False) -> None:
        super().__init__(message)
        self.unsupported = unsupported


class DownloadCancelled(MediaDownloadError):
    pass


def _default_ydl_factory(options: dict[str, Any]) -> object:
    from yt_dlp import YoutubeDL

    return YoutubeDL(options)


def _looks_unsupported(message: str) -> bool:
    folded = message.casefold()
    markers = (
        "drm",
        "members-only",
        "membership required",
        "not entitled",
        "purchase required",
        "premium only",
        "需要会员",
        "付费",
    )
    return any(marker in folded for marker in markers)


class MediaDownloader:
    def __init__(
        self,
        config: WebVideoConfig,
        *,
        ydl_factory: Callable[[dict[str, Any]], object] = _default_ydl_factory,
        js_runtimes: JsRuntimeOptions | None = None,
    ) -> None:
        self.config = config
        self._ydl_factory = ydl_factory
        self.js_runtimes = (
            detect_js_runtimes() if js_runtimes is None else dict(js_runtimes)
        )

    def item_cache_dir(self, item_id: int) -> Path:
        return self.config.cache_dir / str(item_id)

    def _files(self, item_id: int) -> list[Path]:
        directory = self.item_cache_dir(item_id)
        if not directory.is_dir():
            return []
        return sorted(
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.stat().st_size > 0
            and path.suffix.casefold() in MEDIA_EXTENSIONS
            and not path.name.endswith((".part", ".ytdl"))
        )

    def cached_files(self, item_id: int) -> list[Path]:
        directory = self.item_cache_dir(item_id)
        if not (directory / ".download-complete").is_file():
            return []
        return self._files(item_id)

    def _options(
        self,
        directory: Path,
        *,
        progress_hook: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "format": "bestaudio/best",
            "outtmpl": str(directory / "media.%(ext)s"),
            "noplaylist": True,
            "continuedl": True,
            "retries": self.config.download_retries,
            "fragment_retries": self.config.download_retries,
            "concurrent_fragment_downloads": 4,
            "ignoreerrors": False,
            "quiet": True,
            "no_warnings": False,
            "writesubtitles": False,
            "writeautomaticsub": False,
            "impersonate": ImpersonateTarget.from_str("chrome"),
            "progress_hooks": [progress_hook],
        }
        if self.config.cookie_file.is_file():
            options["cookiefile"] = str(self.config.cookie_file)
        if self.js_runtimes:
            options["js_runtimes"] = self.js_runtimes
        return options

    def download(
        self,
        item: dict[str, Any],
        *,
        on_progress: Callable[[float], None],
        should_stop: Callable[[], bool],
    ) -> list[Path]:
        item_id = int(item["id"])
        cached = self.cached_files(item_id)
        if cached:
            on_progress(100)
            return cached
        directory = self.item_cache_dir(item_id)
        directory.mkdir(parents=True, exist_ok=True)

        def progress_hook(event: dict[str, Any]) -> None:
            if should_stop():
                raise DownloadCancelled("任务已停止")
            status = event.get("status")
            if status == "finished":
                on_progress(100)
                return
            downloaded = float(event.get("downloaded_bytes") or 0)
            total = float(
                event.get("total_bytes")
                or event.get("total_bytes_estimate")
                or 0
            )
            if total > 0:
                on_progress(downloaded / total * 100)

        options = self._options(directory, progress_hook=progress_hook)
        headers = item.get("request_headers")
        if isinstance(headers, dict) and headers:
            options["http_headers"] = headers
        media_kind = str(item.get("media_kind") or "")
        if media_kind in {"direct", "hls", "dash"}:
            target_url = str(item.get("url") or item.get("webpage_url") or "")
        else:
            target_url = str(item.get("webpage_url") or item.get("url") or "")
        if not target_url:
            raise MediaDownloadError("视频没有可下载链接")
        try:
            with self._ydl_factory(options) as ydl:  # type: ignore[attr-defined]
                ydl.extract_info(target_url, download=True)  # type: ignore[attr-defined]
        except DownloadCancelled:
            raise
        except Exception as exc:
            message = str(exc)
            raise MediaDownloadError(
                message or "媒体下载失败",
                unsupported=_looks_unsupported(message),
            ) from exc
        files = self._files(item_id)
        if not files:
            raise MediaDownloadError("下载结束但没有找到可转录的媒体文件")
        (directory / ".download-complete").touch()
        return files

    def cleanup(self, item_id: int) -> None:
        shutil.rmtree(self.item_cache_dir(item_id), ignore_errors=True)
