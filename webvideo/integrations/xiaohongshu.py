from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from webvideo.auth import CookieStore
from webvideo.browser_backends import (
    ScraplingBackend,
    StealthBackendError,
    import_chrome_cookies,
)
from webvideo.config import WebVideoConfig
from webvideo.extractors.browser import BrowserExtractor
from webvideo.integrations.base import LoginCheck, QRChallenge, QRPollResult
from webvideo.integrations.base import IntegrationAuthRequired
from webvideo.models import DiscoveryOutcome, VideoCandidate


XHS_ROOT = "https://www.xiaohongshu.com/"
AUTH_COOKIE_NAMES = frozenset({"web_session", "web_session_t", "galaxy_creator_session_id"})
INITIAL_STATE_MARKER = "window.__INITIAL_STATE__="
JS_UNDEFINED_VALUE_RE = re.compile(
    r"(?P<prefix>[:,\[])\s*undefined(?=\s*[,}\]])"
)


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def initial_state_from_xhs_html(html: str) -> dict[str, Any] | None:
    try:
        start = html.index(INITIAL_STATE_MARKER) + len(INITIAL_STATE_MARKER)
        end = html.index("</script>", start)
    except ValueError:
        return None
    raw = html[start:end].strip().removesuffix(";")
    raw = JS_UNDEFINED_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}null",
        raw,
    )
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _first_url(value: Any) -> str:
    preferred = ("url_default", "urlDefault", "url")
    for current in _walk(value):
        for key in preferred:
            raw = current.get(key)
            values = raw if isinstance(raw, list) else [raw]
            for item in values:
                text = str(item or "")
                if text.startswith(("http://", "https://")):
                    return text
    return ""


IMAGE_SUFFIXES = (".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp")
VIDEO_URL_KEYS = frozenset(
    {"master_url", "masterurl", "origin_video_key", "originvideokey"}
)
STREAM_CONTAINER_KEYS = frozenset(
    {"av1", "h264", "h265", "h266", "media", "stream", "video_list"}
)


def _is_image_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    resource = unquote(f"{parts.path}?{parts.query}").casefold()
    return (
        "webpic" in host
        or "image" in host
        or any(marker in resource for marker in IMAGE_SUFFIXES)
        or "format=webp" in resource
        or "nc_n_webp" in resource
    )


def _valid_video_url(value: Any) -> str:
    url = str(value or "")
    if not url.startswith(("http://", "https://")) or _is_image_url(url):
        return ""
    return url


def _video_url(value: Any, *, inside_stream: bool = False) -> str:
    if isinstance(value, list):
        for item in value:
            found = _video_url(item, inside_stream=inside_stream)
            if found:
                return found
        return ""
    if not isinstance(value, dict):
        return ""

    for key, raw in value.items():
        folded = str(key).casefold()
        if folded in VIDEO_URL_KEYS or (inside_stream and folded == "url"):
            values = raw if isinstance(raw, list) else [raw]
            for item in values:
                found = _valid_video_url(item)
                if found:
                    return found

    for key, nested in value.items():
        folded = str(key).casefold()
        if folded in {"avatar", "cover", "image", "image_list", "imagelist"}:
            continue
        found = _video_url(
            nested,
            inside_stream=inside_stream or folded in STREAM_CONTAINER_KEYS,
        )
        if found:
            return found
    return ""


def _duration_seconds(value: Any) -> float:
    for current in _walk(value):
        for key in ("duration", "duration_ms", "durationMs"):
            if key not in current:
                continue
            try:
                duration = float(current[key] or 0)
            except (TypeError, ValueError):
                continue
            return duration / 1000 if duration > 10_000 else duration
    return 0


def candidates_from_xhs_payload(
    payload: dict[str, Any],
    source_page: str,
    *,
    expected_note_id: str = "",
    allow_page_fallback: bool = False,
) -> list[VideoCandidate]:
    result: dict[str, VideoCandidate] = {}
    for current in _walk(payload):
        card = current.get("note_card") or current.get("noteCard")
        note = card if isinstance(card, dict) else current
        note_id = str(
            note.get("note_id")
            or note.get("noteId")
            or (
                note.get("id")
                if "note" in " ".join(map(str, note.keys())).casefold()
                else ""
            )
            or ""
        )
        if not note_id or (expected_note_id and note_id != expected_note_id):
            continue
        note_type = str(note.get("type") or note.get("note_type") or "").casefold()
        video = note.get("video")
        if note_type not in {"video", "normal_video"} and not isinstance(video, dict):
            continue
        media = video if isinstance(video, dict) else note
        webpage_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        xsec_token = str(
            note.get("xsec_token")
            or note.get("xsecToken")
            or current.get("xsec_token")
            or current.get("xsecToken")
            or ""
        )
        if xsec_token:
            xsec_source = str(
                note.get("xsec_source")
                or note.get("xsecSource")
                or current.get("xsec_source")
                or current.get("xsecSource")
                or "pc_user"
            )
            webpage_url += "?" + urlencode(
                {"xsec_token": xsec_token, "xsec_source": xsec_source}
            )
        direct_url = _video_url(media)
        if not direct_url and not allow_page_fallback:
            continue
        user = note.get("user") or note.get("author")
        author = (
            str(user.get("nickname") or user.get("name") or "")
            if isinstance(user, dict)
            else ""
        )
        title = str(
            note.get("display_title")
            or note.get("displayTitle")
            or note.get("title")
            or note.get("desc")
            or note_id
        )
        cover = _first_url(note.get("cover") or media)
        candidate = VideoCandidate.create(
            extractor="XiaoHongShu",
            media_id=note_id,
            url=direct_url or webpage_url,
            webpage_url=webpage_url,
            source_page=source_page,
            title=title,
            author=author,
            duration_seconds=_duration_seconds(media),
            thumbnail_url=cover,
            media_kind="direct" if direct_url else "video",
            request_headers={"Referer": XHS_ROOT} if direct_url else {},
        )
        result[candidate.identity] = candidate
    return list(result.values())


class XiaohongshuQRBrowser:
    def __init__(self, config: WebVideoConfig, cookie_store: CookieStore) -> None:
        self.config = config
        self.cookie_store = cookie_store
        self._session: Any = None
        self._context: Any = None
        self._page: Any = None
        self._key = ""

    async def start(self) -> QRChallenge:
        await self.close()
        try:
            from scrapling.engines._browsers._stealth import AsyncStealthySession
        except ImportError as exc:
            raise RuntimeError("缺少 Scrapling，无法生成小红书二维码") from exc
        import secrets

        try:
            profile = self.config.browser_profile_dir / "xiaohongshu-qr"
            self._session = AsyncStealthySession(
                headless=True,
                real_chrome=self.config.browser_executable.is_file(),
                executable_path=(
                    str(self.config.browser_executable)
                    if self.config.browser_executable.is_file()
                    else None
                ),
                user_data_dir=str(profile),
                cookies=self.cookie_store.playwright_cookies_for(XHS_ROOT),
                google_search=False,
                disable_resources=False,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                retries=1,
                timeout=int(self.config.request_timeout_seconds * 1000),
            )
            await self._session.start()
            self._context = self._session.context
            if self._context is None:
                raise RuntimeError("小红书隐身浏览器没有创建上下文")
            await BrowserExtractor(
                self.config, cookie_store=self.cookie_store
            )._apply_pending_site_data_clear(self._context)
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            loop = asyncio.get_running_loop()
            challenge_data: asyncio.Future[dict[str, Any]] = loop.create_future()

            async def capture(response: Any) -> None:
                if urlsplit(str(response.url)).path != "/api/sns/web/v1/login/qrcode/create":
                    return
                try:
                    payload = await response.json()
                except Exception:
                    return
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict) and not challenge_data.done():
                    challenge_data.set_result(data)

            def on_response(response: Any) -> None:
                asyncio.create_task(capture(response))

            self._page.on("response", on_response)
            await self._page.goto(
                "https://www.xiaohongshu.com/login",
                wait_until="domcontentloaded",
                timeout=int(self.config.request_timeout_seconds * 1000),
            )
            data = await asyncio.wait_for(challenge_data, timeout=20)
            self._key = str(data.get("qr_id") or secrets.token_urlsafe(18))
            qr_url = str(data.get("url") or data.get("code") or "")
            if not qr_url:
                raise RuntimeError("小红书二维码接口没有返回二维码内容")
            return QRChallenge(self._key, qr_url)
        except Exception:
            await self.close()
            raise

    async def poll(self, challenge: QRChallenge) -> QRPollResult:
        if not self._context or challenge.key != self._key:
            return QRPollResult("failed", "小红书二维码会话已失效")
        cookies = await self._context.cookies()
        authenticated = any(
            str(item.get("name") or "").casefold() in AUTH_COOKIE_NAMES
            and bool(item.get("value"))
            for item in cookies
        )
        if authenticated:
            self.cookie_store.merge_playwright(cookies)
            return QRPollResult("success", "小红书登录成功", username="小红书用户")
        if self._page is None or self._page.is_closed():
            return QRPollResult("failed", "小红书二维码窗口已关闭")
        body = str(await self._page.locator("body").inner_text() or "")
        if "二维码已失效" in body or "刷新二维码" in body:
            return QRPollResult("expired", "小红书二维码已过期")
        return QRPollResult("waiting_qr", "等待扫码")

    async def close(self) -> None:
        context, session = self._context, self._session
        self._context = None
        self._page = None
        self._session = None
        self._key = ""
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
        elif context is not None:
            try:
                await context.close()
            except Exception:
                pass


class XiaohongshuIntegration:
    name = "XiaoHongShu"
    platform = "小红书"
    auth_domains = ("xiaohongshu.com",)
    auth_cookie_names = tuple(AUTH_COOKIE_NAMES)
    authenticate_before_discovery = True
    # A copied web_session cookie is only a login candidate. Xiaohongshu can
    # reject it after device/risk checks, so it must not auto-close the visible
    # login browser before a real request has validated the session.
    verify_browser_login_automatically = False

    def __init__(self, config: WebVideoConfig, cookie_store: CookieStore) -> None:
        self.config = config
        self.cookie_store = cookie_store
        self._stealth = ScraplingBackend(config, cookie_store)
        self._qr_browser: XiaohongshuQRBrowser | None = None

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").casefold()
        return host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")

    def normalize(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.path == "/login":
            redirect = (parse_qs(parts.query).get("redirectPath") or [""])[0]
            if redirect:
                target = unquote(redirect)
                if target.startswith(("http://", "https://")):
                    return target
        return url

    @staticmethod
    def _note_id(url: str) -> str:
        pieces = [value for value in urlsplit(url).path.split("/") if value]
        if len(pieces) == 2 and pieces[0] in {"red_video", "explore"}:
            return pieces[1]
        if len(pieces) == 3 and pieces[:2] == ["discovery", "item"]:
            return pieces[2]
        return ""

    @staticmethod
    def _is_collection(url: str) -> bool:
        return urlsplit(url).path.startswith("/user/profile/")

    @staticmethod
    def _is_favorites(url: str) -> bool:
        query = parse_qs(urlsplit(url).query)
        return "fav" in (query.get("tab") or [])

    def prepare_login(self) -> LoginCheck:
        current = self.check_login()
        if current.status == "valid":
            return current
        if (
            not self.config.auto_import_xiaohongshu_chrome
            or self.cookie_store.requires_reauth(self.auth_domains)
        ):
            return current
        import_chrome_cookies(
            self.cookie_store,
            domains=self.auth_domains,
            required_cookie_names=AUTH_COOKIE_NAMES,
        )
        return self.check_login()

    def check_login(self, cookies: dict[str, str] | None = None) -> LoginCheck:
        values = cookies or self.cookie_store.cookies_for_url(XHS_ROOT)
        valid = any(values.get(name) for name in AUTH_COOKIE_NAMES)
        return LoginCheck("valid" if valid else "invalid")

    def discover(self, *_: Any) -> None:
        return None

    async def discover_async(
        self,
        url: str,
        on_candidate: Callable[[VideoCandidate], None],
        should_stop: Callable[[], bool],
    ) -> DiscoveryOutcome | None:
        note_id = self._note_id(url)
        collection = self._is_collection(url)
        favorites = self._is_favorites(url)
        if not note_id and not collection:
            return None
        try:
            captured = await self._stealth.capture(
                url,
                xhr_pattern=r"/api/sns/web/v\d+/",
                scroll=collection,
                should_stop=should_stop,
            )
        except StealthBackendError:
            return None
        if urlsplit(captured.url).path == "/login":
            raise IntegrationAuthRequired("小红书登录状态已失效")
        found: dict[str, VideoCandidate] = {}
        payloads = list(captured.payloads)
        initial_state = initial_state_from_xhs_html(str(getattr(captured, "html", "")))
        if initial_state is not None:
            payloads.append((captured.url, initial_state))
        for response_url, payload in payloads:
            folded = response_url.casefold()
            if favorites and not any(
                marker in folded for marker in ("collect", "fav", "favorite")
            ):
                continue
            for candidate in candidates_from_xhs_payload(
                payload,
                url,
                expected_note_id=note_id,
                allow_page_fallback=collection,
            ):
                found[candidate.identity] = candidate
        for candidate in found.values():
            on_candidate(candidate)
        return (
            DiscoveryOutcome(len(found), collection, False)
            if found
            else None
        )

    async def create_qrcode(self) -> QRChallenge:
        await self.close_qrcode()
        self._qr_browser = XiaohongshuQRBrowser(self.config, self.cookie_store)
        return await self._qr_browser.start()

    async def poll_qrcode(self, challenge: QRChallenge) -> QRPollResult:
        if self._qr_browser is None:
            return QRPollResult("failed", "小红书二维码会话已失效")
        return await self._qr_browser.poll(challenge)

    async def close_qrcode(self) -> None:
        browser, self._qr_browser = self._qr_browser, None
        if browser is not None:
            await browser.close()

    async def refresh_download(self, item: dict[str, object]) -> VideoCandidate:
        webpage_url = str(item.get("webpage_url") or "")
        media_id = str(item.get("media_id") or "")
        source_page = str(item.get("source_page") or webpage_url)

        async def discover(target: str) -> list[VideoCandidate]:
            found: list[VideoCandidate] = []
            try:
                await self.discover_async(target, found.append, lambda: False)
            except Exception:
                return []
            return found

        if (
            not parse_qs(urlsplit(webpage_url).query).get("xsec_token")
            and self._is_collection(source_page)
        ):
            listed = await discover(source_page)
            listing = next(
                (candidate for candidate in listed if candidate.media_id == media_id),
                None,
            )
            if listing is not None:
                webpage_url = listing.webpage_url

        found = await discover(webpage_url)
        direct = next(
            (
                candidate
                for candidate in found
                if candidate.media_id == media_id
                and candidate.media_kind == "direct"
            ),
            None,
        )
        if direct is not None:
            return direct
        return VideoCandidate.create(
            extractor="XiaoHongShu",
            media_id=media_id,
            url=webpage_url,
            webpage_url=webpage_url,
            source_page=source_page,
            title=str(item.get("title") or media_id),
            author=str(item.get("author") or ""),
            duration_seconds=item.get("duration_seconds") or 0,
            thumbnail_url=str(item.get("thumbnail_url") or ""),
            media_kind="video",
        )
