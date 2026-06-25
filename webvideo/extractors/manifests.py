from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlsplit


DIRECT_EXTENSIONS = frozenset(
    {".aac", ".flac", ".flv", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm"}
)
SUBTITLE_EXTENSIONS = frozenset({".ass", ".srt", ".ttml", ".vtt"})


def classify_media_resource(url: str, content_type: str = "") -> str | None:
    path = PurePosixPath(urlsplit(url).path.casefold())
    suffix = path.suffix
    mime = content_type.split(";", 1)[0].strip().casefold()
    if suffix in {".cmfa", ".cmfv", ".m4s", ".ts"}:
        return None
    if suffix == ".m3u8" or mime in {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
    }:
        return "hls"
    if suffix == ".mpd" or mime == "application/dash+xml":
        return "dash"
    if suffix in SUBTITLE_EXTENSIONS or mime.startswith("text/vtt"):
        return "subtitle"
    if suffix in DIRECT_EXTENSIONS or mime.startswith(("audio/", "video/")):
        return "direct"
    return None
