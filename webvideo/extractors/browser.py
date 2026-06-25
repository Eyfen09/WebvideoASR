from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from webvideo.auth import CookieStore
from webvideo.config import WebVideoConfig
from webvideo.models import VideoCandidate

from .manifests import classify_media_resource


LOAD_MORE_RE = re.compile(
    r"^(?:load|show|view)\s+more$|加载更多|显示更多|查看更多|下一页|next$",
    re.I,
)


@dataclass(frozen=True)
class BrowserDiscovery:
    candidates: list[VideoCandidate]
    links: list[str]
    title: str


class BrowserExtractor:
    def __init__(
        self,
        config: WebVideoConfig,
        *,
        cookie_store: CookieStore | None = None,
    ) -> None:
        self.config = config
        self.cookie_store = cookie_store or CookieStore(config.cookie_file)

    async def _export_cookies(self, context: Any) -> None:
        cookies = await context.cookies()
        self.cookie_store.merge_playwright(cookies)

    async def _apply_pending_site_data_clear(self, context: Any) -> None:
        domains = self.cookie_store.pending_browser_clear_domains()
        if not domains:
            return
        for domain in domains:
            pattern = re.compile(rf"(?:^|\.){re.escape(domain)}$", re.I)
            await context.clear_cookies(domain=pattern)

        page = context.pages[0] if context.pages else await context.new_page()
        session = await context.new_cdp_session(page)
        try:
            for domain in domains:
                hosts = {domain, f"www.{domain}"}
                for host in hosts:
                    for scheme in ("https", "http"):
                        try:
                            await session.send(
                                "Storage.clearDataForOrigin",
                                {
                                    "origin": f"{scheme}://{host}",
                                    "storageTypes": "all",
                                },
                            )
                        except Exception:
                            # Cookies are the login-critical state. Some Chrome
                            # builds reject clearing storage for an unused origin.
                            pass
        finally:
            await session.detach()
        self.cookie_store.complete_browser_clear(domains)

    async def _wait_for_login(
        self,
        page: Any,
        context: Any,
        should_stop: Any,
        *,
        forced: bool,
        get_user_action: Callable[[], str],
        check_login: Callable[[list[dict[str, Any]]], Awaitable[bool]] | None = None,
        on_login_success: Callable[[], None] | None = None,
    ) -> str:
        del page, forced
        while not should_stop():
            action = get_user_action()
            if action in {"continue", "skip"}:
                return action
            if check_login is not None:
                try:
                    if await check_login(await context.cookies()):
                        if on_login_success is not None:
                            on_login_success()
                        return "continue"
                except Exception:
                    pass
            await asyncio.sleep(1.0)
        return "stop"

    async def discover(
        self,
        url: str,
        should_stop: Any,
        *,
        on_browser_ready: Callable[[], None],
        get_user_action: Callable[[], str],
        on_authenticated: Callable[[], Awaitable[bool]],
        allow_scroll: bool,
        check_login: Callable[[list[dict[str, Any]]], Awaitable[bool]] | None = None,
        on_login_success: Callable[[], None] | None = None,
    ) -> BrowserDiscovery:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 Playwright，无法启动页面解析") from exc

        resource_map: dict[str, tuple[str, dict[str, str]]] = {}
        links: set[str] = set()
        title = ""
        async with async_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "user_data_dir": str(self.config.browser_profile_dir),
                "headless": False,
                "viewport": {"width": 1380, "height": 900},
                "timeout": 30_000,
                "args": ["--no-first-run", "--no-default-browser-check"],
            }
            if self.config.browser_executable.is_file():
                launch_options["executable_path"] = str(
                    self.config.browser_executable
                )
            context = await playwright.chromium.launch_persistent_context(
                **launch_options
            )
            await self._apply_pending_site_data_clear(context)
            stored_cookies = self.cookie_store.playwright_cookies_for(url)
            if stored_cookies:
                await context.add_cookies(stored_cookies)
            page = context.pages[0] if context.pages else await context.new_page()

            def on_response(response: Any) -> None:
                resource_url = str(response.url)
                if not resource_url.startswith(("http://", "https://")):
                    return
                content_type = str(response.headers.get("content-type", ""))
                kind = classify_media_resource(resource_url, content_type)
                if kind in {"direct", "hls", "dash"}:
                    request_headers = {
                        key: value
                        for key, value in response.request.headers.items()
                        if key.casefold() in {"referer", "user-agent", "origin"}
                    }
                    resource_map[resource_url] = (kind, request_headers)

            page.on("response", on_response)
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(self.config.request_timeout_seconds * 1000),
                )
                on_browser_ready()
                action = await self._wait_for_login(
                    page,
                    context,
                    should_stop,
                    forced=True,
                    get_user_action=get_user_action,
                    check_login=check_login,
                    on_login_success=on_login_success,
                )
                if action == "stop" or should_stop():
                    await self._export_cookies(context)
                    return BrowserDiscovery([], [], await page.title())
                if action == "continue":
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(self.config.request_timeout_seconds * 1000),
                    )
                await self._export_cookies(context)
                if await on_authenticated():
                    if on_login_success is not None:
                        on_login_success()
                    return BrowserDiscovery([], [], await page.title())

                if not allow_scroll:
                    await asyncio.sleep(self.config.scroll_wait_seconds)
                stable = 0
                previous_signature: tuple[int, int] | None = None
                rounds = self.config.max_scroll_rounds if allow_scroll else 1
                for _ in range(rounds):
                    if should_stop():
                        break
                    discovered = await page.evaluate(
                        """
                        () => Array.from(document.querySelectorAll(
                            'a[href], iframe[src], video[src], audio[src], source[src]'
                        )).map(el => el.href || el.src).filter(Boolean)
                        """
                    )
                    for value in discovered or []:
                        absolute = urljoin(page.url, str(value))
                        if absolute.startswith(("http://", "https://")):
                            links.add(absolute)

                    clicked = False
                    if allow_scroll:
                        clicked = await page.evaluate(
                            """
                            () => {
                              const visible = el => {
                                const r = el.getBoundingClientRect();
                                const s = getComputedStyle(el);
                                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden';
                              };
                              const re = /^(?:load|show|view)\\s+more$|加载更多|显示更多|查看更多|下一页|^next$/i;
                              const el = Array.from(document.querySelectorAll('button, a'))
                                .find(node => visible(node) && re.test((node.innerText || '').trim()));
                              if (!el) return false;
                              el.click();
                              return true;
                            }
                            """
                        )
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)"
                        )
                        await asyncio.sleep(self.config.scroll_wait_seconds)
                    height = int(
                        await page.evaluate("document.body.scrollHeight") or 0
                    )
                    signature = (len(links) + len(resource_map), height)
                    if signature == previous_signature and not clicked:
                        stable += 1
                    else:
                        stable = 0
                    previous_signature = signature
                    if stable >= self.config.stable_scroll_rounds:
                        break
                title = await page.title()
                await self._export_cookies(context)
            finally:
                await context.close()

        candidates: list[VideoCandidate] = []
        for resource_url, (kind, headers) in resource_map.items():
            filename = Path(urlsplit(resource_url).path).name or f"{kind} 媒体"
            candidates.append(
                VideoCandidate.create(
                    extractor=f"browser-{kind}",
                    media_id="",
                    url=resource_url,
                    webpage_url=resource_url,
                    source_page=url,
                    title=f"{title or '网页媒体'} · {filename}",
                    media_kind=kind,
                    request_headers=headers,
                )
            )
        return BrowserDiscovery(candidates, sorted(links), title)
