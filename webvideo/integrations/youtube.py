from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from webvideo.auth import CookieStore
from webvideo.browser_backends import import_chrome_cookies
from webvideo.config import WebVideoConfig
from webvideo.integrations.base import LoginCheck


YOUTUBE_ROOT = "https://www.youtube.com/"
AUTH_COOKIE_NAMES = frozenset(
    {
        "sapisid",
        "__secure-1papisid",
        "__secure-3papisid",
        "sid",
        "hsid",
        "ssid",
    }
)


class YoutubeIntegration:
    """Session preparation for yt-dlp; discovery remains fully generic."""

    name = "YouTube"
    platform = "YouTube"
    auth_domains = ("youtube.com", "google.com")
    auth_cookie_names = tuple(AUTH_COOKIE_NAMES)
    # Google intentionally blocks sign-in inside automation-controlled Chrome.
    disable_visible_browser = True

    def __init__(self, config: WebVideoConfig, cookie_store: CookieStore) -> None:
        self.config = config
        self.cookie_store = cookie_store

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").casefold()
        return host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com")

    def normalize(self, url: str) -> str:
        return url

    def prepare_session(self) -> None:
        if not self.config.auto_import_youtube_chrome:
            return
        if self.check_login().status == "valid":
            return
        import_chrome_cookies(
            self.cookie_store,
            domains=self.auth_domains,
            required_cookie_names=AUTH_COOKIE_NAMES,
        )

    def check_login(self, cookies: dict[str, str] | None = None) -> LoginCheck:
        values = cookies or self.cookie_store.cookies_for_url(YOUTUBE_ROOT)
        names = {str(name).casefold() for name, value in values.items() if value}
        return LoginCheck("valid" if names & AUTH_COOKIE_NAMES else "invalid")

    def discover(self, *_: Any) -> None:
        return None
