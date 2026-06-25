from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "spm_id_from",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)
SIGNED_KEY_RE = re.compile(
    r"^(?:auth|expire|expires|key|policy|signature|sig|token|x-amz-)", re.I
)
PLACEHOLDER_TITLES = frozenset(
    {
        "unknown",
        "unknown title",
        "untitled",
        "未命名视频",
        "未知标题",
        "标题未知",
    }
)


def canonicalize_url(url: str, *, strip_signatures: bool = False) -> str:
    parts = urlsplit(url.strip())
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        folded = key.casefold()
        if folded in TRACKING_KEYS:
            continue
        if strip_signatures and SIGNED_KEY_RE.search(folded):
            continue
        query.append((key, value))
    path = parts.path or "/"
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            path,
            urlencode(query, doseq=True),
            "",
        )
    )


def format_duration(value: Any) -> str:
    try:
        total = max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return ""
    if total == 0:
        return ""
    hours, remaining = divmod(total, 3600)
    minutes, seconds = divmod(remaining, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def is_placeholder_title(title: str, *, media_id: str = "", url: str = "") -> bool:
    value = str(title or "").strip()
    if not value or value.casefold() in PLACEHOLDER_TITLES:
        return True
    if media_id and value.casefold() == media_id.strip().casefold():
        return True
    if url and canonicalize_url(value) == canonicalize_url(url):
        return True
    folded = value.casefold()
    return folded.startswith(("media-video-", "media-audio-", "browser-direct"))


def stable_identity(extractor: str, media_id: str, url: str) -> str:
    if extractor.strip() and media_id.strip():
        return f"{extractor.casefold()}:{media_id.strip()}"
    normalized = canonicalize_url(url, strip_signatures=True)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"url:{digest}"


@dataclass(frozen=True)
class VideoCandidate:
    identity: str
    extractor: str
    media_id: str
    url: str
    webpage_url: str
    source_page: str
    title: str
    author: str = ""
    duration_seconds: int = 0
    thumbnail_url: str = ""
    media_kind: str = "video"
    request_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        extractor: str,
        media_id: str,
        url: str,
        webpage_url: str = "",
        source_page: str = "",
        title: str = "",
        author: str = "",
        duration_seconds: Any = 0,
        thumbnail_url: str = "",
        media_kind: str = "video",
        request_headers: dict[str, str] | None = None,
    ) -> VideoCandidate:
        try:
            duration = max(0, int(float(duration_seconds or 0)))
        except (TypeError, ValueError):
            duration = 0
        effective_url = webpage_url or url
        return cls(
            identity=stable_identity(extractor, media_id, effective_url),
            extractor=extractor or "generic",
            media_id=media_id,
            url=url,
            webpage_url=effective_url,
            source_page=source_page or effective_url,
            title=(title or media_id or effective_url or "未命名视频").strip(),
            author=author.strip(),
            duration_seconds=duration,
            thumbnail_url=thumbnail_url,
            media_kind=media_kind,
            request_headers=request_headers or {},
        )


@dataclass(frozen=True)
class DiscoveryOutcome:
    count: int
    is_collection: bool
    needs_browser: bool
    error: str = ""
