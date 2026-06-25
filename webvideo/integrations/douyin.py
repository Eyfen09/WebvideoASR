from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

from webvideo.auth import CookieStore
from webvideo.browser_backends import ScraplingBackend, StealthBackendError
from webvideo.config import WebVideoConfig
from webvideo.integrations.base import LoginCheck
from webvideo.models import DiscoveryOutcome, VideoCandidate

from .douyin_browser import (
    AUTH_COOKIE_NAMES,
    candidates_from_post_payload,
    discover_douyin_profile,
)


class DouyinIntegration:
    name = "Douyin"
    platform = "抖音"
    auth_domains = ("douyin.com",)
    auth_cookie_names = ("sessionid", "sessionid_ss", "sid_guard")
    authenticate_before_discovery = True
    verify_browser_login_automatically = False

    def __init__(self, config: WebVideoConfig, cookie_store: CookieStore) -> None:
        self.config = config
        self.cookie_store = cookie_store
        self._stealth = ScraplingBackend(config, cookie_store)

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").casefold()
        return host == "douyin.com" or host.endswith(".douyin.com")

    def normalize(self, url: str) -> str:
        if not self.matches(url):
            return url
        query = parse_qs(urlsplit(url).query)
        for key in ("modal_id", "aweme_id"):
            values = query.get(key) or []
            if values and values[0].isdigit():
                return f"https://www.douyin.com/video/{values[0]}"
        return url

    def discover(
        self,
        url: str,
        on_candidate: Callable[[VideoCandidate], None],
        should_stop: Callable[[], bool],
    ) -> DiscoveryOutcome | None:
        del url, on_candidate, should_stop
        return None

    @staticmethod
    def _profile_id(url: str) -> str:
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() not in {
            "douyin.com",
            "www.douyin.com",
        }:
            return ""
        pieces = [item for item in parts.path.split("/") if item]
        return pieces[1] if len(pieces) == 2 and pieces[0] == "user" else ""

    async def discover_async(
        self,
        url: str,
        on_candidate: Callable[[VideoCandidate], None],
        should_stop: Callable[[], bool],
    ) -> DiscoveryOutcome | None:
        sec_user_id = self._profile_id(url)
        pieces = [item for item in urlsplit(url).path.split("/") if item]
        video_id = (
            pieces[1]
            if len(pieces) == 2 and pieces[0] == "video" and pieces[1].isdigit()
            else ""
        )
        if not sec_user_id and not video_id:
            return None

        try:
            captured = await self._stealth.capture(
                url,
                xhr_pattern=r"/aweme/v1/(?:web/)?(?:aweme/post|aweme/detail)/",
                scroll=bool(sec_user_id),
                should_stop=should_stop,
            )
        except StealthBackendError:
            captured = None

        found: dict[str, VideoCandidate] = {}
        if captured is not None:
            for _, payload in captured.payloads:
                for candidate in candidates_from_post_payload(
                    payload,
                    url,
                    expected_user_id=sec_user_id,
                ):
                    if video_id and candidate.media_id != video_id:
                        continue
                    found[candidate.identity] = candidate
        if found:
            for candidate in found.values():
                on_candidate(candidate)
            return DiscoveryOutcome(len(found), bool(sec_user_id), False)

        if sec_user_id:
            return await discover_douyin_profile(
                self.config,
                self.cookie_store,
                url,
                sec_user_id,
                on_candidate,
                should_stop,
            )
        return None

    def check_login(self, cookies: dict[str, str] | None = None) -> LoginCheck:
        values = cookies or self.cookie_store.cookies_for_url(
            "https://www.douyin.com/"
        )
        valid = any(values.get(name) for name in AUTH_COOKIE_NAMES)
        return LoginCheck("valid" if valid else "invalid")

    async def refresh_download(self, item: dict[str, object]) -> VideoCandidate | None:
        found: list[VideoCandidate] = []
        await self.discover_async(
            str(item.get("webpage_url") or ""),
            found.append,
            lambda: False,
        )
        media_id = str(item.get("media_id") or "")
        return next(
            (candidate for candidate in found if candidate.media_id == media_id),
            found[0] if found else None,
        )
