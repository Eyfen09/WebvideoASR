from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from webvideo.auth import CookieStore
from webvideo.config import WebVideoConfig
from webvideo.integrations.base import (
    IntegrationAuthRequired,
    IntegrationError,
)
from webvideo.models import DiscoveryOutcome, VideoCandidate


AUTH_COOKIE_NAMES = frozenset({"sessionid", "sessionid_ss", "sid_guard"})
POST_API_PATH = "/aweme/v1/web/aweme/post/"


def _launch_options(config: WebVideoConfig, *, headless: bool) -> dict[str, Any]:
    options: dict[str, Any] = {
        "headless": headless,
        "args": ["--no-first-run", "--no-default-browser-check"],
    }
    if config.browser_executable.is_file():
        options["executable_path"] = str(config.browser_executable)
    return options


def _authenticated(cookies: list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("name") or "").casefold() in AUTH_COOKIE_NAMES
        and bool(item.get("value"))
        for item in cookies
    )


def candidates_from_post_payload(
    payload: dict[str, Any],
    source_page: str,
    *,
    expected_user_id: str = "",
) -> list[VideoCandidate]:
    result: list[VideoCandidate] = []
    items: list[Any] = []
    listed = payload.get("aweme_list")
    if isinstance(listed, list):
        items.extend(listed)
    detail = payload.get("aweme_detail")
    if isinstance(detail, dict):
        items.append(detail)
    details = payload.get("aweme_details")
    if isinstance(details, list):
        items.extend(details)
    for item in items:
        if not isinstance(item, dict):
            continue
        media_id = str(item.get("aweme_id") or "")
        if not media_id:
            continue
        author_data = item.get("author")
        author_id = (
            str(
                author_data.get("sec_uid")
                or author_data.get("uid")
                or author_data.get("unique_id")
                or ""
            )
            if isinstance(author_data, dict)
            else ""
        )
        if expected_user_id and author_id and author_id != expected_user_id:
            continue
        author = (
            str(author_data.get("nickname") or "")
            if isinstance(author_data, dict)
            else ""
        )
        video = item.get("video")
        video_data = video if isinstance(video, dict) else {}
        try:
            duration_ms = float(video_data.get("duration") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
        cover_data = video_data.get("cover")
        cover = cover_data if isinstance(cover_data, dict) else {}
        cover_urls = cover.get("url_list")
        thumbnail = (
            str(cover_urls[0])
            if isinstance(cover_urls, list) and cover_urls
            else ""
        )
        webpage_url = f"https://www.douyin.com/video/{media_id}"
        direct_url = ""
        bit_rates = video_data.get("bit_rate")
        if isinstance(bit_rates, list):
            ranked = sorted(
                (value for value in bit_rates if isinstance(value, dict)),
                key=lambda value: float(
                    value.get("bit_rate") or value.get("data_size") or 0
                ),
                reverse=True,
            )
            for value in ranked:
                address = value.get("play_addr")
                urls = address.get("url_list") if isinstance(address, dict) else None
                if isinstance(urls, list) and urls:
                    direct_url = str(urls[0])
                    break
        if not direct_url:
            address = video_data.get("play_addr")
            urls = address.get("url_list") if isinstance(address, dict) else None
            if isinstance(urls, list) and urls:
                direct_url = str(urls[0])
        result.append(
            VideoCandidate.create(
                extractor="Douyin",
                media_id=media_id,
                url=direct_url or webpage_url,
                webpage_url=webpage_url,
                source_page=source_page,
                title=str(item.get("desc") or media_id),
                author=author,
                duration_seconds=duration_ms / 1000,
                thumbnail_url=thumbnail,
                media_kind="direct" if direct_url else "ytdlp",
                request_headers={"Referer": "https://www.douyin.com/"},
            )
        )
    return result


async def discover_douyin_profile(
    config: WebVideoConfig,
    cookie_store: CookieStore,
    url: str,
    sec_user_id: str,
    on_candidate: Callable[[VideoCandidate], None],
    should_stop: Callable[[], bool],
) -> DiscoveryOutcome:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise IntegrationError("缺少 Playwright，无法解析抖音主页") from exc

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    capture_tasks: set[asyncio.Task[Any]] = set()
    count = 0
    seen: set[str] = set()
    seen_cursors: set[str] = set()
    incomplete = False
    page_count = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            **_launch_options(config, headless=True)
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}
        )
        stored = cookie_store.playwright_cookies_for(url)
        if stored:
            await context.add_cookies(stored)
        page = await context.new_page()

        async def capture(response: Any) -> None:
            parts = urlsplit(str(response.url))
            if parts.path != POST_API_PATH:
                return
            response_user = (parse_qs(parts.query).get("sec_user_id") or [""])[0]
            if response_user != sec_user_id:
                return
            try:
                payload = await response.json()
            except Exception:
                return
            if isinstance(payload, dict):
                await queue.put(payload)

        def on_response(response: Any) -> None:
            task = asyncio.create_task(capture(response))
            capture_tasks.add(task)
            task.add_done_callback(capture_tasks.discard)

        page.on("response", on_response)
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(config.request_timeout_seconds * 1000),
            )
            while not should_stop():
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=12)
                except TimeoutError:
                    if not count and (
                        "/login" in page.url
                        or not _authenticated(await context.cookies())
                    ):
                        raise IntegrationAuthRequired("抖音主页需要登录")
                    incomplete = True
                    break
                status_code = int(payload.get("status_code") or 0)
                if status_code != 0:
                    raise IntegrationError(
                        f"抖音作品列表响应失败 ({status_code})"
                    )
                page_count += 1
                cursor = str(payload.get("max_cursor") or "")
                if cursor and cursor in seen_cursors:
                    incomplete = True
                    break
                if cursor:
                    seen_cursors.add(cursor)
                for candidate in candidates_from_post_payload(
                    payload, url, expected_user_id=sec_user_id
                ):
                    if candidate.identity in seen:
                        continue
                    seen.add(candidate.identity)
                    on_candidate(candidate)
                    count += 1
                if not payload.get("has_more"):
                    break
                if page_count >= 100:
                    incomplete = True
                    break
                for _ in range(3):
                    await page.evaluate(
                        """
                        () => {
                          const list = document.querySelector('[data-e2e="user-post-list"]');
                          const last = list?.lastElementChild;
                          if (last) last.scrollIntoView({block: 'end'});
                          const scroll = document.querySelector('[data-e2e="scroll-list"]');
                          if (scroll) scroll.scrollTop = scroll.scrollHeight;
                          window.scrollTo(0, document.body.scrollHeight);
                        }
                        """
                    )
                    await page.mouse.wheel(0, 1400)
                    await asyncio.sleep(0.6)
                    if not queue.empty():
                        break
            cookie_store.merge_playwright(await context.cookies())
        finally:
            if capture_tasks:
                await asyncio.gather(*capture_tasks, return_exceptions=True)
            await context.close()
            await browser.close()

    return DiscoveryOutcome(
        count=count,
        is_collection=True,
        needs_browser=count == 0,
        error=("抖音作品列表未完整加载" if incomplete else "")
        if count
        else "抖音主页没有返回作品",
    )
