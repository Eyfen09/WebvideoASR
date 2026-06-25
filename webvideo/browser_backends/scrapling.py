from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from webvideo.auth import CookieStore
from webvideo.config import WebVideoConfig


_PROFILE_LOCK = threading.Lock()


class StealthBackendError(RuntimeError):
    pass


class StealthBackendUnavailable(StealthBackendError):
    pass


@dataclass(frozen=True)
class StealthCaptureResult:
    url: str
    title: str
    html: str
    payloads: tuple[tuple[str, dict[str, Any]], ...]
    cookies: tuple[dict[str, Any], ...]


class ScraplingBackend:
    """Small boundary around Scrapling used only by site integrations."""

    def __init__(self, config: WebVideoConfig, cookie_store: CookieStore) -> None:
        self.config = config
        self.cookie_store = cookie_store
        self.profile_dir = config.browser_profile_dir / "scrapling"

    @property
    def enabled(self) -> bool:
        return self.config.use_scrapling_backend

    async def capture(
        self,
        url: str,
        *,
        xhr_pattern: str,
        scroll: bool = False,
        should_stop: Callable[[], bool] = lambda: False,
        page_action: Callable[[Any], Any] | None = None,
    ) -> StealthCaptureResult:
        if not self.enabled:
            raise StealthBackendUnavailable("Scrapling 隐身浏览器已禁用")
        try:
            from scrapling.engines._browsers._stealth import AsyncStealthySession
        except ImportError as exc:
            raise StealthBackendUnavailable("缺少 Scrapling 隐身浏览器依赖") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        cookies = self.cookie_store.playwright_cookies_for(url)
        matcher = re.compile(xhr_pattern)
        live_payloads: list[tuple[str, dict[str, Any]]] = []
        capture_tasks: set[asyncio.Task[Any]] = set()

        async def capture_response(response: Any) -> None:
            if not matcher.search(str(response.url)):
                return
            if str(response.request.resource_type) not in {"xhr", "fetch"}:
                return
            try:
                payload = await response.json()
            except Exception:
                return
            if isinstance(payload, dict):
                live_payloads.append((str(response.url), payload))

        async def setup(page: Any) -> None:
            def on_response(response: Any) -> None:
                task = asyncio.create_task(capture_response(response))
                capture_tasks.add(task)
                task.add_done_callback(capture_tasks.discard)

            page.on("response", on_response)

        async def automate(page: Any) -> None:
            if page_action is not None:
                result = page_action(page)
                if hasattr(result, "__await__"):
                    await result
            if not scroll:
                return
            stable = 0
            previous_height = -1
            rounds = min(self.config.max_scroll_rounds, 100)
            for _ in range(rounds):
                if should_stop():
                    break
                height = int(await page.evaluate("document.body.scrollHeight") or 0)
                await page.evaluate(
                    """
                    () => {
                      const list = document.querySelector(
                        '[data-e2e="user-post-list"], [data-e2e="scroll-list"]'
                      );
                      if (list) list.scrollTop = list.scrollHeight;
                      const last = document.querySelector(
                        '[data-e2e="user-post-list"] > :last-child'
                      );
                      if (last) last.scrollIntoView({block: 'end'});
                      window.scrollTo(0, document.body.scrollHeight);
                    }
                    """
                )
                await page.mouse.wheel(0, 1400)
                await asyncio.sleep(self.config.scroll_wait_seconds)
                new_height = int(
                    await page.evaluate("document.body.scrollHeight") or 0
                )
                if new_height == height == previous_height:
                    stable += 1
                else:
                    stable = 0
                previous_height = new_height
                if stable >= self.config.stable_scroll_rounds:
                    break

        await asyncio.to_thread(_PROFILE_LOCK.acquire)
        session: Any = None
        try:
            session = AsyncStealthySession(
                headless=True,
                real_chrome=self.config.browser_executable.is_file(),
                executable_path=(
                    str(self.config.browser_executable)
                    if self.config.browser_executable.is_file()
                    else None
                ),
                user_data_dir=str(self.profile_dir),
                cookies=cookies,
                google_search=False,
                disable_resources=False,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                retries=1,
                timeout=int(self.config.request_timeout_seconds * 1000),
            )
            await session.start()
            context = session.context
            if context is None:
                raise RuntimeError("Scrapling 没有创建浏览器上下文")
            page = context.pages[0] if context.pages else await context.new_page()
            await setup(page)
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self.config.request_timeout_seconds * 1000),
            )
            await automate(page)
            await page.wait_for_timeout(800)
            if capture_tasks:
                await asyncio.gather(*capture_tasks, return_exceptions=True)
            final_url = str(page.url)
            title = str(await page.title() or "")
            html = str(await page.content() or "")
            response_cookies = tuple(dict(item) for item in await context.cookies())
        except Exception as exc:
            raise StealthBackendError(str(exc) or "Scrapling 页面捕获失败") from exc
        finally:
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass
            _PROFILE_LOCK.release()

        payloads: list[tuple[str, dict[str, Any]]] = list(live_payloads)
        if response_cookies:
            self.cookie_store.merge_playwright(list(response_cookies))
        return StealthCaptureResult(
            url=final_url,
            title=title,
            html=html,
            payloads=tuple(payloads),
            cookies=response_cookies,
        )
