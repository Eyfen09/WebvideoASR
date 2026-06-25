from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx

from webvideo.auth import CookieStore
from webvideo.browser_backends import ScraplingBackend, StealthBackendError
from webvideo.config import WebVideoConfig
from webvideo.integrations.base import (
    IntegrationAuthRequired,
    IntegrationError,
    LoginCheck,
)
from webvideo.models import DiscoveryOutcome, VideoCandidate


KS_ROOT = "https://www.kuaishou.com/"
KS_GRAPHQL = "https://www.kuaishou.com/graphql"
AUTH_COOKIE_NAMES = frozenset(
    {
        "kuaishou.server.webday7_st",
        "kuaishou.server.web_st",
        "passToken",
    }
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    ),
    "Referer": KS_ROOT,
    "Origin": "https://www.kuaishou.com",
    "Content-Type": "application/json",
}
VIDEO_DETAIL_QUERY = """
query VisionVideoDetail($photoId: String!) {
  visionVideoDetail(photoId: $photoId) {
    status
    photo {
      id
      duration
      caption
      coverUrl
      photoUrl
      photoH265Url
      manifest {
        adaptationSet {
          id
          duration
          representation {
            id
            url
            width
            height
            avgBitrate
          }
        }
      }
    }
    llsid
  }
}
"""
PROFILE_PHOTO_LIST_QUERY = """
query visionProfilePhotoList($pcursor: String, $userId: String, $page: String) {
  visionProfilePhotoList(pcursor: $pcursor, userId: $userId, page: $page) {
    result
    llsid
    webPageArea
    feeds {
      type
      author {
        id
        name
      }
      photo {
        id
        duration
        caption
        coverUrl
        photoUrl
        photoH265Url
        manifest {
          adaptationSet {
            id
            duration
            representation {
              id
              url
              width
              height
              avgBitrate
              size
              type
            }
          }
        }
      }
    }
    hostName
    pcursor
  }
}
"""


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _media_url(photo: dict[str, Any]) -> str:
    for key in ("photoH265Url", "photoUrl", "playUrl", "url"):
        value = str(photo.get(key) or "")
        if value.startswith(("http://", "https://")):
            return value
    for key in ("mainMvUrls", "mvUrls"):
        values = photo.get(key)
        if not isinstance(values, list):
            continue
        ranked = sorted(
            (item for item in values if isinstance(item, dict)),
            key=lambda item: float(
                item.get("height") or item.get("avgBitrate") or item.get("size") or 0
            ),
            reverse=True,
        )
        for item in ranked:
            value = str(item.get("url") or "")
            if value.startswith(("http://", "https://")):
                return value
    manifest = photo.get("manifest")
    for current in _walk(manifest):
        value = str(current.get("url") or "")
        if value.startswith(("http://", "https://")):
            return value
    return ""


def candidates_from_kuaishou_payload(
    payload: dict[str, Any],
    source_page: str,
    *,
    expected_photo_id: str = "",
    expected_user_id: str = "",
) -> list[VideoCandidate]:
    result: dict[str, VideoCandidate] = {}
    for current in _walk(payload):
        nested_photo = current.get("photo")
        nested_author = current.get("author") or current.get("user")
        if isinstance(nested_photo, dict) and isinstance(nested_author, dict):
            photo = {**nested_photo, "author": nested_author}
        else:
            photo = current
        if not any(
            key in photo
            for key in (
                "caption",
                "coverUrl",
                "photoUrl",
                "photoH265Url",
                "mvUrls",
                "mainMvUrls",
                "manifest",
            )
        ):
            continue
        photo_id = str(photo.get("photoId") or photo.get("id") or "")
        if not photo_id or (expected_photo_id and photo_id != expected_photo_id):
            continue
        direct_url = _media_url(photo)
        if not direct_url:
            continue
        user = photo.get("user") or photo.get("author")
        user_id = str(
            photo.get("userId")
            or photo.get("authorId")
            or (user.get("id") if isinstance(user, dict) else "")
            or ""
        )
        if expected_user_id and user_id and user_id != expected_user_id:
            continue
        author = str(
            photo.get("userName")
            or photo.get("authorName")
            or (user.get("name") if isinstance(user, dict) else "")
            or ""
        )
        try:
            duration = float(photo.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration > 10_000:
            duration /= 1000
        webpage_url = f"https://www.kuaishou.com/short-video/{photo_id}"
        candidate = VideoCandidate.create(
            extractor="Kuaishou",
            media_id=photo_id,
            url=direct_url,
            webpage_url=webpage_url,
            source_page=source_page,
            title=str(photo.get("caption") or photo.get("title") or photo_id),
            author=author,
            duration_seconds=duration,
            thumbnail_url=str(photo.get("coverUrl") or ""),
            media_kind="direct",
            request_headers={"Referer": KS_ROOT},
        )
        previous = result.get(candidate.identity)
        if previous is not None and previous.author and not candidate.author:
            continue
        result[candidate.identity] = candidate
    return list(result.values())


class KuaishouIntegration:
    name = "Kuaishou"
    platform = "快手"
    auth_domains = ("kuaishou.com",)
    auth_cookie_names = tuple(AUTH_COOKIE_NAMES)
    authenticate_before_discovery = True
    visible_browser_auth_only = True
    disable_generic_fallback = True

    def __init__(
        self,
        config: WebVideoConfig,
        cookie_store: CookieStore,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.cookie_store = cookie_store
        self.transport = transport
        self._stealth = ScraplingBackend(config, cookie_store)

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").casefold()
        return host == "kuaishou.com" or host.endswith(".kuaishou.com")

    def normalize(self, url: str) -> str:
        return url

    @staticmethod
    def _photo_id(url: str) -> str:
        pieces = [value for value in urlsplit(url).path.split("/") if value]
        return pieces[1] if len(pieces) == 2 and pieces[0] == "short-video" else ""

    @staticmethod
    def _profile_id(url: str) -> str:
        pieces = [value for value in urlsplit(url).path.split("/") if value]
        return pieces[1] if len(pieces) == 2 and pieces[0] == "profile" else ""

    def discover(self, *_: Any) -> None:
        return None

    def check_login(self, cookies: dict[str, str] | None = None) -> LoginCheck:
        values = cookies or self.cookie_store.cookies_for_url(KS_ROOT)
        folded = {
            str(name).casefold(): str(value)
            for name, value in values.items()
            if value
        }
        valid = any(folded.get(name.casefold()) for name in AUTH_COOKIE_NAMES)
        return LoginCheck("valid" if valid else "invalid")

    def _client(self, url: str = KS_ROOT) -> httpx.Client:
        return httpx.Client(
            timeout=self.config.request_timeout_seconds,
            headers=HEADERS,
            cookies=self.cookie_store.cookies_for_url(url),
            transport=self.transport,
            follow_redirects=True,
        )

    def _invalidate_login(self) -> None:
        removed = self.cookie_store.delete_domains(self.auth_domains)
        self.cookie_store.mark_reauth(self.auth_domains)
        self.cookie_store.mark_browser_clear([*self.auth_domains, *removed])

    def _graphql(self, operation: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.post(KS_GRAPHQL, json=operation)
        except httpx.HTTPError as exc:
            raise IntegrationError(f"快手接口请求失败：{exc}") from exc

        if response.status_code in {401, 403}:
            self._invalidate_login()
            raise IntegrationAuthRequired("快手登录状态已失效")

        try:
            payload = response.json()
        except ValueError:
            payload = None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and isinstance(errors, list) and errors:
            messages = [
                str(item.get("message") or "")
                for item in errors
                if isinstance(item, dict)
            ]
            message = "；".join(value for value in messages if value) or "未知错误"
            folded = message.casefold()
            if any(
                marker in folded
                for marker in ("login", "unauthorized", "permission", "登录")
            ):
                self._invalidate_login()
                raise IntegrationAuthRequired("快手登录状态已失效")
            raise IntegrationError(f"快手接口返回错误：{message}")

        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise IntegrationError(f"快手接口请求失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise IntegrationError("快手接口响应无效")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise IntegrationError("快手接口响应缺少 data")
        return payload

    def _resolve_short_photo_id(self, url: str) -> str:
        try:
            with self._client(url) as client:
                response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise IntegrationError(f"快手短链接解析失败：{exc}") from exc
        return self._photo_id(str(response.url))

    async def _capture_candidates(
        self,
        url: str,
        *,
        photo_id: str = "",
        profile_id: str = "",
        should_stop: Callable[[], bool],
    ) -> list[VideoCandidate]:
        try:
            captured = await self._stealth.capture(
                url,
                xhr_pattern=r"(?:/graphql|/rest/[^?]*(?:photo|feed|profile))",
                scroll=bool(profile_id),
                should_stop=should_stop,
            )
        except StealthBackendError:
            return []
        final_photo_id = photo_id or self._photo_id(captured.url)
        found: dict[str, VideoCandidate] = {}
        for _, payload in captured.payloads:
            for candidate in candidates_from_kuaishou_payload(
                payload,
                url,
                expected_photo_id=final_photo_id,
                expected_user_id=profile_id,
            ):
                found[candidate.identity] = candidate
        return list(found.values())

    async def _discover_video(
        self,
        url: str,
        photo_id: str,
        on_candidate: Callable[[VideoCandidate], None],
        should_stop: Callable[[], bool],
    ) -> DiscoveryOutcome:
        if should_stop():
            return DiscoveryOutcome(0, False, False)
        operation = {
            "operationName": "VisionVideoDetail",
            "variables": {"photoId": photo_id},
            "query": VIDEO_DETAIL_QUERY,
        }
        direct_error = ""
        try:
            payload = await asyncio.to_thread(self._graphql, operation)
            candidates = candidates_from_kuaishou_payload(
                payload,
                url,
                expected_photo_id=photo_id,
            )
        except IntegrationAuthRequired:
            raise
        except IntegrationError as exc:
            direct_error = str(exc)
            candidates = []
        if not candidates and not should_stop():
            candidates = await self._capture_candidates(
                url,
                photo_id=photo_id,
                should_stop=should_stop,
            )
        for candidate in candidates:
            if should_stop():
                break
            on_candidate(candidate)
        if candidates:
            return DiscoveryOutcome(len(candidates), False, False)
        if should_stop():
            return DiscoveryOutcome(0, False, False)
        raise IntegrationError(direct_error or "未获取到快手视频信息")

    @staticmethod
    def _profile_cursor(payload: dict[str, Any]) -> str:
        data = payload.get("data")
        listing = (
            data.get("visionProfilePhotoList")
            if isinstance(data, dict)
            else None
        )
        if not isinstance(listing, dict):
            return ""
        return str(listing.get("pcursor") or "").strip()

    async def _discover_profile(
        self,
        url: str,
        profile_id: str,
        on_candidate: Callable[[VideoCandidate], None],
        should_stop: Callable[[], bool],
    ) -> DiscoveryOutcome:
        found: dict[str, VideoCandidate] = {}
        cursor = ""
        seen_cursors: set[str] = set()
        direct_error = ""
        for _ in range(self.config.max_scroll_rounds):
            if should_stop():
                break
            operation = {
                "operationName": "visionProfilePhotoList",
                "variables": {
                    "pcursor": cursor,
                    "userId": profile_id,
                    "page": "profile",
                },
                "query": PROFILE_PHOTO_LIST_QUERY,
            }
            try:
                payload = await asyncio.to_thread(self._graphql, operation)
            except IntegrationAuthRequired:
                raise
            except IntegrationError as exc:
                direct_error = str(exc)
                break
            page_candidates = candidates_from_kuaishou_payload(
                payload,
                url,
                expected_user_id=profile_id,
            )
            for candidate in page_candidates:
                if should_stop():
                    break
                if candidate.identity in found:
                    continue
                found[candidate.identity] = candidate
                on_candidate(candidate)
            next_cursor = self._profile_cursor(payload)
            if (
                not next_cursor
                or next_cursor.casefold() in {"no_more", "nomore", "null", "-1"}
                or next_cursor == cursor
                or next_cursor in seen_cursors
            ):
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        if not found and not should_stop():
            captured = await self._capture_candidates(
                url,
                profile_id=profile_id,
                should_stop=should_stop,
            )
            for candidate in captured:
                if candidate.identity in found:
                    continue
                found[candidate.identity] = candidate
                on_candidate(candidate)
        if found or should_stop():
            return DiscoveryOutcome(
                len(found),
                True,
                False,
                direct_error if found else "",
            )
        raise IntegrationError(direct_error or "未获取到快手主页视频")

    async def discover_async(
        self,
        url: str,
        on_candidate: Callable[[VideoCandidate], None],
        should_stop: Callable[[], bool],
    ) -> DiscoveryOutcome | None:
        photo_id = self._photo_id(url)
        profile_id = self._profile_id(url)
        if not photo_id and not profile_id and "/f/" not in urlsplit(url).path:
            return None
        if profile_id:
            return await self._discover_profile(
                url,
                profile_id,
                on_candidate,
                should_stop,
            )
        if not photo_id:
            photo_id = await asyncio.to_thread(self._resolve_short_photo_id, url)
        if not photo_id:
            raise IntegrationError("无法从快手链接提取视频 ID")
        return await self._discover_video(
            url,
            photo_id,
            on_candidate,
            should_stop,
        )

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
