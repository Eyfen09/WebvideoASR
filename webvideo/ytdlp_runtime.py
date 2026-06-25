from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit


JsRuntimeOptions = dict[str, dict[str, str]]
DEFAULT_FALLBACK_DIRS = (
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path.home() / ".local" / "bin",
)


def detect_js_runtimes(
    locator: Callable[[str], str | None] | None = None,
    *,
    fallback_dirs: Iterable[Path] | None = None,
) -> JsRuntimeOptions:
    find = locator or shutil.which
    directories = (
        tuple(fallback_dirs)
        if fallback_dirs is not None
        else (() if locator is not None else DEFAULT_FALLBACK_DIRS)
    )
    for name in ("node", "deno"):
        path = find(name)
        if path:
            return {name: {"path": path}}
        for directory in directories:
            for executable in (name, f"{name}.exe"):
                candidate = directory / executable
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return {name: {"path": str(candidate)}}
    return {}


def is_youtube_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold()
    return host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com")
