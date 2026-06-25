from __future__ import annotations

import base64
import hashlib
import math
import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit

import httpx

from webvideo.auth import CookieStore
from webvideo.models import DiscoveryOutcome, VideoCandidate

from .base import (
    IntegrationAuthRequired,
    IntegrationError,
    LoginCheck,
    QRChallenge,
    QRPollResult,
)


NAV_API = "https://api.bilibili.com/x/web-interface/nav"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
SPACE_API = "https://api.bilibili.com/x/space/wbi/arc/search"
FAVORITE_API = "https://api.bilibili.com/x/v3/fav/resource/list"
QR_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}
SPACE_RE = re.compile(r"^/(\d+)(?:/(?:upload/)?video)?/?$")
VIDEO_RE = re.compile(r"^/video/(BV[0-9A-Za-z]+)", re.I)
MIXIN_KEY_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


def _duration_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parts = [int(item) for item in text.split(":")]
    except ValueError:
        return 0
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0


class BilibiliIntegration:
    name = "Bilibili"
    platform = "Bilibili"
    auth_domains = ("bilibili.com",)
    auth_cookie_names = ("SESSDATA", "DedeUserID")
    authenticate_before_discovery = True

    def __init__(
        self,
        cookie_store: CookieStore,
        *,
        timeout: float = 45.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cookie_store = cookie_store
        self.timeout = timeout
        self.transport = transport
        self._wbi_key = ""
        self._wbi_key_time = 0.0
        self._key_lock = threading.RLock()

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").casefold()
        return host in {"bilibili.com", "b23.tv"} or host.endswith(
            ".bilibili.com"
        )

    def normalize(self, url: str) -> str:
        return url

    def _client(
        self,
        url: str = "https://www.bilibili.com/",
        *,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            headers=HEADERS,
            cookies=cookies if cookies is not None else self.cookie_store.cookies_for_url(url),
            transport=self.transport,
            follow_redirects=True,
        )

    @staticmethod
    def _payload(response: httpx.Response, action: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IntegrationError(f"{action}失败：{exc}") from exc
        if not isinstance(data, dict):
            raise IntegrationError(f"{action}响应无效")
        code = int(data.get("code") or 0)
        if code == -101:
            raise IntegrationAuthRequired(f"{action}需要登录")
        if code != 0:
            raise IntegrationError(
                f"{action}失败 ({code})：{data.get('message') or '未知错误'}"
            )
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise IntegrationError(f"{action}响应缺少 data")
        return payload

    def _get_wbi_key(self, client: httpx.Client) -> str:
        with self._key_lock:
            if self._wbi_key and time.time() - self._wbi_key_time < 30 * 60:
                return self._wbi_key
            payload = self._payload(client.get(NAV_API), "获取 WBI 签名")
            images = payload.get("wbi_img") if isinstance(payload.get("wbi_img"), dict) else {}
            lookup = ""
            for key in ("img_url", "sub_url"):
                filename = str(images.get(key) or "").rsplit("/", 1)[-1]
                lookup += filename.split(".", 1)[0]
            if len(lookup) < 64:
                raise IntegrationError("Bilibili WBI 签名信息不完整")
            self._wbi_key = "".join(lookup[index] for index in MIXIN_KEY_TABLE)[:32]
            self._wbi_key_time = time.time()
            return self._wbi_key

    def _sign(self, client: httpx.Client, params: dict[str, Any]) -> dict[str, str]:
        values = {**params, "wts": round(time.time())}
        cleaned = {
            key: "".join(char for char in str(value) if char not in "!'()*")
            for key, value in sorted(values.items())
        }
        query = urlencode(cleaned)
        cleaned["w_rid"] = hashlib.md5(
            f"{query}{self._get_wbi_key(client)}".encode()
        ).hexdigest()
        return cleaned

    @staticmethod
    def _space_id(url: str) -> str:
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != "space.bilibili.com":
            return ""
        match = SPACE_RE.fullmatch(parts.path)
        return match.group(1) if match else ""

    @staticmethod
    def _favorite_id(url: str) -> str:
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != "space.bilibili.com":
            return ""
        if not parts.path.rstrip("/").endswith("/favlist"):
            return ""
        fid = (parse_qs(parts.query).get("fid") or [""])[0]
        return fid if fid.isdigit() else ""

    @staticmethod
    def _video_id(url: str) -> str:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
        if host != "bilibili.com" and not host.endswith(".bilibili.com"):
            return ""
        match = VIDEO_RE.match(parts.path)
        return match.group(1) if match else ""

    def _discover_video(
        self,
        url: str,
        bvid: str,
        on_candidate: Callable[[VideoCandidate], None],
    ) -> DiscoveryOutcome:
        with self._client(url) as client:
            payload = self._payload(
                client.get(VIEW_API, params={"bvid": bvid}),
                "获取 Bilibili 视频信息",
            )
        resolved_bvid = str(payload.get("bvid") or bvid)
        owner = payload.get("owner")
        author = (
            str(owner.get("name") or "") if isinstance(owner, dict) else ""
        )
        webpage_url = f"https://www.bilibili.com/video/{resolved_bvid}"
        on_candidate(
            VideoCandidate.create(
                extractor="BiliBili",
                media_id=resolved_bvid,
                url=webpage_url,
                webpage_url=webpage_url,
                source_page=url,
                title=str(payload.get("title") or resolved_bvid),
                author=author,
                duration_seconds=_duration_seconds(payload.get("duration")),
                thumbnail_url=str(payload.get("pic") or ""),
                media_kind="ytdlp",
            )
        )
        return DiscoveryOutcome(1, False, False)

    def _discover_favorite(
        self,
        url: str,
        fid: str,
        on_candidate: Callable[[VideoCandidate], None],
        should_stop: Callable[[], bool],
    ) -> DiscoveryOutcome:
        count = 0
        page = 1
        seen: set[str] = set()
        with self._client(url) as client:
            while not should_stop():
                payload = self._payload(
                    client.get(
                        FAVORITE_API,
                        params={
                            "media_id": fid,
                            "pn": page,
                            "ps": 20,
                            "keyword": "",
                            "order": "mtime",
                            "type": 0,
                            "tid": 0,
                            "platform": "web",
                        },
                    ),
                    f"获取 Bilibili 收藏夹第 {page} 页",
                )
                medias = payload.get("medias")
                videos = medias if isinstance(medias, list) else []
                for item in videos:
                    if should_stop() or not isinstance(item, dict):
                        break
                    bvid = str(item.get("bvid") or "")
                    if not bvid or bvid in seen:
                        continue
                    seen.add(bvid)
                    upper = item.get("upper")
                    author = (
                        str(upper.get("name") or "")
                        if isinstance(upper, dict)
                        else ""
                    )
                    webpage_url = f"https://www.bilibili.com/video/{bvid}"
                    on_candidate(
                        VideoCandidate.create(
                            extractor="BiliBili",
                            media_id=bvid,
                            url=webpage_url,
                            webpage_url=webpage_url,
                            source_page=url,
                            title=str(item.get("title") or bvid),
                            author=author,
                            duration_seconds=_duration_seconds(item.get("duration")),
                            thumbnail_url=str(item.get("cover") or ""),
                            media_kind="ytdlp",
                        )
                    )
                    count += 1
                if not payload.get("has_more") or not videos:
                    break
                page += 1
        return DiscoveryOutcome(
            count=count,
            is_collection=True,
            needs_browser=count == 0,
            error="" if count else "Bilibili 收藏夹没有返回视频",
        )

    def discover(
        self,
        url: str,
        on_candidate: Callable[[VideoCandidate], None],
        should_stop: Callable[[], bool],
    ) -> DiscoveryOutcome | None:
        bvid = self._video_id(url)
        if bvid:
            return self._discover_video(url, bvid, on_candidate)
        fid = self._favorite_id(url)
        if fid:
            return self._discover_favorite(url, fid, on_candidate, should_stop)
        mid = self._space_id(url)
        if not mid:
            return None
        count = 0
        page = 1
        with self._client(url) as client:
            while not should_stop():
                params = {
                    "keyword": "",
                    "mid": mid,
                    "order": (parse_qs(urlsplit(url).query).get("order") or ["pubdate"])[0],
                    "order_avoided": "true",
                    "platform": "web",
                    "pn": page,
                    "ps": 30,
                    "tid": 0,
                    "web_location": 1550101,
                    "dm_img_list": "[]",
                    "dm_img_str": base64.b64encode(b"abcdefghijklmnop")[:-2].decode(),
                    "dm_cover_img_str": base64.b64encode(
                        b"abcdefghijklmnopqrstuvwxyzABCDEFGH"
                    )[:-2].decode(),
                    "dm_img_inter": '{"ds":[],"wh":[6093,6631,31],"of":[430,760,380]}',
                }
                payload = self._payload(
                    client.get(SPACE_API, params=self._sign(client, params)),
                    f"获取 Bilibili 空间第 {page} 页",
                )
                listing = payload.get("list") if isinstance(payload.get("list"), dict) else {}
                videos = listing.get("vlist") if isinstance(listing.get("vlist"), list) else []
                for item in videos:
                    if should_stop() or not isinstance(item, dict):
                        break
                    bvid = str(item.get("bvid") or "")
                    if not bvid:
                        continue
                    webpage_url = f"https://www.bilibili.com/video/{bvid}"
                    on_candidate(
                        VideoCandidate.create(
                            extractor="BiliBili",
                            media_id=bvid,
                            url=webpage_url,
                            webpage_url=webpage_url,
                            source_page=url,
                            title=str(item.get("title") or bvid),
                            author=str(item.get("author") or ""),
                            duration_seconds=_duration_seconds(
                                item.get("duration") or item.get("length")
                            ),
                            thumbnail_url=str(item.get("pic") or ""),
                            media_kind="ytdlp",
                        )
                    )
                    count += 1
                page_info = payload.get("page") if isinstance(payload.get("page"), dict) else {}
                total = int(page_info.get("count") or count)
                page_size = max(1, int(page_info.get("ps") or 30))
                if page >= math.ceil(total / page_size) or not videos:
                    break
                page += 1
        return DiscoveryOutcome(
            count=count,
            is_collection=True,
            needs_browser=count == 0,
            error="" if count else "Bilibili 空间没有返回视频",
        )

    def check_login(self, cookies: dict[str, str] | None = None) -> LoginCheck:
        try:
            with self._client(cookies=cookies) as client:
                payload = self._payload(client.get(NAV_API), "校验 Bilibili 登录态")
        except IntegrationError:
            return LoginCheck("unknown")
        if not payload.get("isLogin"):
            return LoginCheck("invalid")
        return LoginCheck("valid", str(payload.get("uname") or "已登录用户"))

    def create_qrcode(self) -> QRChallenge:
        with self._client(cookies={}) as client:
            payload = self._payload(client.get(QR_GENERATE_API), "生成 Bilibili 二维码")
        key = str(payload.get("qrcode_key") or "")
        url = str(payload.get("url") or "")
        if not key or not url:
            raise IntegrationError("生成 Bilibili 二维码响应不完整")
        return QRChallenge(key, url)

    def poll_qrcode(self, challenge: QRChallenge) -> QRPollResult:
        with self._client(cookies={}) as client:
            response = client.get(QR_POLL_API, params={"qrcode_key": challenge.key})
            payload = self._payload(response, "轮询 Bilibili 二维码")
        code = int(payload.get("code") or 0)
        if code == 86090:
            return QRPollResult("scanned", "已扫码，请在手机上确认")
        if code == 86101:
            return QRPollResult("waiting_qr", "等待扫码")
        if code == 86038:
            return QRPollResult("expired", "二维码已过期")
        if code != 0:
            return QRPollResult("failed", str(payload.get("message") or "登录失败"))
        cookies = {cookie.name: cookie.value for cookie in response.cookies.jar}
        callback = parse_qs(urlparse(str(payload.get("url") or "")).query)
        for name, values in callback.items():
            if values:
                cookies.setdefault(name, values[0])
        try:
            expires = int((callback.get("Expires") or ["0"])[0])
        except ValueError:
            expires = 0
        return QRPollResult(
            "success",
            "Bilibili 登录成功",
            cookies=cookies,
            cookie_domain=".bilibili.com",
            expires=expires,
            username="Bilibili 用户",
        )
