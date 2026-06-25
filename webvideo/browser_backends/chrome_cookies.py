from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from webvideo.auth import CookieStore


def _chrome_profiles(root: Path) -> list[Path]:
    profiles: list[str] = []
    state_path = root / "Local State"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            profile = state.get("profile") if isinstance(state, dict) else None
            if isinstance(profile, dict):
                last_used = str(profile.get("last_used") or "")
                if last_used:
                    profiles.append(last_used)
                info = profile.get("info_cache")
                if isinstance(info, dict):
                    profiles.extend(str(key) for key in info)
        except (OSError, ValueError):
            pass
    profiles.extend(["Default", *(path.name for path in root.glob("Profile *"))])
    unique: list[Path] = []
    for name in profiles:
        path = root / name
        if path.is_dir() and path not in unique:
            unique.append(path)
    return unique


def import_chrome_cookies(
    cookie_store: CookieStore,
    *,
    domains: Iterable[str],
    chrome_root: Path | None = None,
    extractor: Callable[..., Any] | None = None,
    required_cookie_names: Iterable[str] = (),
) -> int:
    """Read Chrome cookies and persist only entries inside requested domains."""

    if extractor is None:
        try:
            from yt_dlp.cookies import extract_cookies_from_browser
        except ImportError:
            return 0
        extractor = extract_cookies_from_browser
    root = chrome_root or (
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    )
    scopes = tuple(str(value).lstrip(".").casefold() for value in domains)
    imported: list[dict[str, object]] = []
    required = {str(value).casefold() for value in required_cookie_names}
    for profile in _chrome_profiles(root):
        profile_imported: list[dict[str, object]] = []
        try:
            jar = extractor("chrome", str(profile))
        except Exception:
            continue
        for item in jar:
            domain = str(getattr(item, "domain", "") or "")
            normalized = domain.lstrip(".").casefold()
            if not any(
                normalized == scope or normalized.endswith(f".{scope}")
                for scope in scopes
            ):
                continue
            name = str(getattr(item, "name", "") or "")
            value = str(getattr(item, "value", "") or "")
            if not name or not value:
                continue
            profile_imported.append(
                {
                    "domain": domain,
                    "name": name,
                    "value": value,
                    "path": str(getattr(item, "path", "/") or "/"),
                    "secure": bool(getattr(item, "secure", True)),
                    "expires": int(getattr(item, "expires", 0) or 0),
                }
            )
        if not profile_imported:
            continue
        imported = profile_imported
        names = {str(item["name"]).casefold() for item in imported}
        if not required or names & required:
            break
    if imported:
        cookie_store.merge_playwright(imported)
    return len(imported)
