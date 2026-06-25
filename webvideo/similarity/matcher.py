from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from webvideo.models import VideoCandidate, is_placeholder_title

from .base import SimilarityEngine


MEDIA_EXTENSIONS = frozenset(
    {".aac", ".flv", ".m4a", ".mp3", ".mp4", ".mov", ".webm", ".m3u8", ".mpd"}
)


def _resource_path(value: str) -> str:
    parts = urlsplit(value)
    path = parts.path
    if PurePosixPath(path).suffix.casefold() not in MEDIA_EXTENSIONS:
        return ""
    return f"{(parts.hostname or '').casefold()}{path}"


def _duration_compatible(left: int, right: int) -> bool:
    if left <= 0 or right <= 0:
        return True
    tolerance = max(3, round(max(left, right) * 0.05))
    return abs(left - right) <= tolerance


def fallback_duplicate_identity(
    candidate: VideoCandidate,
    existing: Iterable[VideoCandidate],
    *,
    engine: SimilarityEngine,
    threshold: float,
) -> str | None:
    """Return an existing identity only for a high-confidence duplicate.

    Low similarity never filters a candidate. Explicitly different stable IDs are
    never merged, regardless of their titles.
    """

    candidate_path = _resource_path(candidate.url)
    for current in existing:
        if candidate.identity == current.identity:
            return current.identity
        if candidate.media_id and current.media_id:
            if candidate.media_id.casefold() == current.media_id.casefold():
                return current.identity
            continue
        current_path = _resource_path(current.url)
        if candidate_path and current_path and candidate_path == current_path:
            return current.identity
        if not _duration_compatible(
            candidate.duration_seconds, current.duration_seconds
        ):
            continue
        if is_placeholder_title(
            candidate.title,
            media_id=candidate.media_id,
            url=candidate.webpage_url or candidate.url,
        ) or is_placeholder_title(
            current.title,
            media_id=current.media_id,
            url=current.webpage_url or current.url,
        ):
            continue
        if threshold <= 0:
            continue
        if engine.score(candidate.title, current.title) >= threshold:
            return current.identity
    return None
