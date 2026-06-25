from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from yt_dlp.networking.impersonate import ImpersonateTarget

from webvideo.models import DiscoveryOutcome, VideoCandidate
from webvideo.ytdlp_runtime import (
    JsRuntimeOptions,
    detect_js_runtimes,
    is_youtube_url,
)


class YtDlpDiscoveryError(RuntimeError):
    pass


def _default_ydl_factory(options: dict[str, Any]) -> object:
    from yt_dlp import YoutubeDL

    return YoutubeDL(options)


class YtDlpExtractor:
    def __init__(
        self,
        cookie_file: Path,
        *,
        ydl_factory: Callable[[dict[str, Any]], object] = _default_ydl_factory,
        js_runtimes: JsRuntimeOptions | None = None,
    ) -> None:
        self.cookie_file = cookie_file
        self._ydl_factory = ydl_factory
        self.js_runtimes = (
            detect_js_runtimes() if js_runtimes is None else dict(js_runtimes)
        )

    def _options(self, *, flat: bool, playlist: bool) -> dict[str, Any]:
        options: dict[str, Any] = {
            "skip_download": True,
            "extract_flat": "in_playlist" if flat else False,
            "lazy_playlist": flat and playlist,
            "noplaylist": not playlist,
            "ignoreerrors": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 45,
            "impersonate": ImpersonateTarget.from_str("chrome"),
        }
        if self.cookie_file.is_file():
            options["cookiefile"] = str(self.cookie_file)
        if self.js_runtimes:
            options["js_runtimes"] = self.js_runtimes
        return options

    @staticmethod
    def _entries(info: dict[str, Any]) -> Iterable[dict[str, Any]]:
        entries = info.get("entries")
        if entries is None:
            yield info
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            nested = entry.get("entries")
            if nested is not None:
                yield from YtDlpExtractor._entries(entry)
            else:
                yield entry

    @staticmethod
    def _candidate(info: dict[str, Any], source_page: str) -> VideoCandidate | None:
        extractor = str(
            info.get("extractor_key")
            or info.get("ie_key")
            or info.get("extractor")
            or "generic"
        )
        media_id = str(info.get("id") or "")
        webpage_url = str(
            info.get("webpage_url")
            or info.get("original_url")
            or info.get("url")
            or ""
        )
        direct_url = str(info.get("url") or webpage_url)
        if not webpage_url.startswith(("http://", "https://")):
            if direct_url.startswith(("http://", "https://")):
                webpage_url = direct_url
            else:
                return None
        return VideoCandidate.create(
            extractor=extractor,
            media_id=media_id,
            url=direct_url if direct_url.startswith(("http://", "https://")) else webpage_url,
            webpage_url=webpage_url,
            source_page=source_page,
            title=str(info.get("title") or info.get("fulltitle") or media_id),
            author=str(
                info.get("creator")
                or info.get("channel")
                or info.get("artist")
                or info.get("uploader")
                or ""
            ),
            duration_seconds=info.get("duration") or 0,
            thumbnail_url=str(info.get("thumbnail") or ""),
            media_kind="ytdlp",
        )

    def discover(
        self,
        url: str,
        on_candidate: Callable[[VideoCandidate], None],
        should_stop: Callable[[], bool],
    ) -> DiscoveryOutcome:
        try:
            with self._ydl_factory(self._options(flat=True, playlist=True)) as ydl:  # type: ignore[attr-defined]
                info = ydl.extract_info(url, download=False)  # type: ignore[attr-defined]
        except Exception as exc:
            message = str(exc).strip() or f"{type(exc).__name__}: yt-dlp 初始化或解析失败"
            return DiscoveryOutcome(0, False, True, message)
        if not isinstance(info, dict):
            if is_youtube_url(url) and not self.js_runtimes:
                error = "未检测到 Node.js 或 Deno；YouTube 解析需要 JavaScript 运行时"
            else:
                error = "yt-dlp 没有返回视频信息"
            return DiscoveryOutcome(0, False, True, error)

        is_collection = info.get("entries") is not None or info.get("_type") in {
            "playlist",
            "multi_video",
        }
        top_extractor = str(
            info.get("extractor_key")
            or info.get("ie_key")
            or info.get("extractor")
            or ""
        ).casefold()
        seen: set[str] = set()
        for entry in self._entries(info):
            if should_stop():
                break
            candidate = self._candidate(entry, url)
            if candidate is None or candidate.identity in seen:
                continue
            seen.add(candidate.identity)
            on_candidate(candidate)
        needs_browser = not seen or "generic" in top_extractor
        return DiscoveryOutcome(
            len(seen), is_collection, needs_browser, "" if seen else "没有发现视频"
        )

    def probe(self, url: str, source_page: str) -> VideoCandidate | None:
        try:
            with self._ydl_factory(self._options(flat=True, playlist=False)) as ydl:  # type: ignore[attr-defined]
                info = ydl.extract_info(url, download=False)  # type: ignore[attr-defined]
        except Exception:
            return None
        if not isinstance(info, dict):
            return None
        return self._candidate(info, source_page)

    @staticmethod
    def is_likely_supported(url: str) -> bool:
        try:
            from yt_dlp.extractor import gen_extractor_classes

            return any(
                cls.IE_NAME != "generic" and cls.suitable(url)
                for cls in gen_extractor_classes()
            )
        except Exception:
            return False
