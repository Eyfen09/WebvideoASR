from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from .auth import AuthCoordinator, CookieStore
from .config import WebVideoConfig
from .extractors.browser import BrowserExtractor
from .extractors.manifests import classify_media_resource
from .extractors.ytdlp import YtDlpExtractor
from .integrations.base import IntegrationAuthRequired, IntegrationError
from .integrations.registry import IntegrationRegistry, default_registry
from .models import DiscoveryOutcome, VideoCandidate
from .repository import WebVideoRepository
from .similarity import (
    JaccardTitleSimilarity,
    SimilarityEngine,
    fallback_duplicate_identity,
)


VIDEO_PATH_HINTS = (
    "/av",
    "/episode",
    "/media/",
    "/play",
    "/reel",
    "/shorts/",
    "/video",
    "/watch",
)
LOGIN_MARKERS = (
    "authentication",
    "cookies",
    "log in",
    "login",
    "sign in",
    "会员",
    "登录",
)


class Resolver:
    def __init__(
        self,
        config: WebVideoConfig,
        repository: WebVideoRepository,
        *,
        ytdlp: YtDlpExtractor | None = None,
        browser: BrowserExtractor | None = None,
        integrations: IntegrationRegistry | None = None,
        auth: AuthCoordinator | None = None,
        cookie_store: CookieStore | None = None,
        similarity_engine: SimilarityEngine | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.cookie_store = cookie_store or CookieStore(config.cookie_file)
        self.auth = auth or AuthCoordinator(
            self.cookie_store,
            poll_interval_seconds=config.qrcode_poll_interval_seconds,
        )
        self.integrations = integrations or default_registry(config, self.cookie_store)
        self.ytdlp = ytdlp or YtDlpExtractor(config.cookie_file)
        self.browser = browser or BrowserExtractor(
            config,
            cookie_store=self.cookie_store,
        )
        self.similarity_engine = similarity_engine or JaccardTitleSimilarity()

    @staticmethod
    def _candidate_link(url: str) -> bool:
        if classify_media_resource(url) is not None:
            return False
        path = urlsplit(url).path.casefold()
        return any(hint in path for hint in VIDEO_PATH_HINTS)

    @staticmethod
    def _needs_enrichment(candidate: VideoCandidate) -> bool:
        title = candidate.title.strip()
        return (
            not candidate.author.strip()
            or candidate.duration_seconds <= 0
            or not title
            or title == candidate.media_id
            or title == candidate.webpage_url
        )

    @staticmethod
    def _login_required(error: str) -> bool:
        folded = error.casefold()
        return any(marker in folded for marker in LOGIN_MARKERS)

    @staticmethod
    def _align_enriched_identity(
        original: VideoCandidate, enriched: VideoCandidate
    ) -> VideoCandidate:
        """Treat a first-part probe as metadata for its parent list entry."""
        original_id = original.media_id.strip()
        enriched_id = enriched.media_id.strip()
        if (
            original_id
            and enriched_id.casefold() == f"{original_id}_p1".casefold()
            and original.extractor.casefold() == enriched.extractor.casefold()
        ):
            return replace(
                enriched,
                identity=original.identity,
                media_id=original.media_id,
                webpage_url=original.webpage_url,
            )
        return enriched

    async def _enrich_candidates(
        self,
        candidates: list[VideoCandidate],
        *,
        source_page: str,
        should_stop: Callable[[], bool],
        add: Callable[[VideoCandidate], None],
    ) -> None:
        pending = [item for item in candidates if self._needs_enrichment(item)]
        if not pending:
            return
        queue: asyncio.Queue[VideoCandidate] = asyncio.Queue()
        for candidate in pending:
            queue.put_nowait(candidate)

        async def worker() -> None:
            while not should_stop():
                try:
                    candidate = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                enriched = None
                for attempt in range(2):
                    enriched = await asyncio.to_thread(
                        self.ytdlp.probe,
                        candidate.webpage_url or candidate.url,
                        source_page,
                    )
                    if enriched is not None or should_stop():
                        break
                    await asyncio.sleep(0.5 * (attempt + 1))
                if enriched is not None:
                    add(self._align_enriched_identity(candidate, enriched))

        worker_count = min(self.config.probe_concurrency, len(pending))
        await asyncio.gather(*(worker() for _ in range(worker_count)))

    async def _try_qrcode_login(
        self,
        task_id: int,
        integration: Any,
        should_stop: Callable[[], bool],
        *,
        skip_saved_login: bool = False,
    ) -> bool:
        prepare_login = (
            None if skip_saved_login else getattr(integration, "prepare_login", None)
        )
        prepared = None
        if callable(prepare_login):
            if inspect.iscoroutinefunction(prepare_login):
                prepared = await prepare_login()
            else:
                prepared = await asyncio.to_thread(prepare_login)
        check_login = (
            None if skip_saved_login else getattr(integration, "check_login", None)
        )
        if prepared is not None:
            check = prepared
        elif callable(check_login):
            check = await asyncio.to_thread(check_login)
        else:
            check = None
        if check is not None:
            if getattr(check, "status", "") == "valid":
                self.auth.set_status(
                    task_id,
                    "reused",
                    "已复用登录状态",
                    platform=str(getattr(integration, "platform", "")),
                    username=str(getattr(check, "username", "")),
                )
                return True
        if self.config.preferred_login_mode != "qrcode":
            return False
        if not callable(getattr(integration, "create_qrcode", None)) or not callable(
            getattr(integration, "poll_qrcode", None)
        ):
            return False
        if skip_saved_login:
            domains = tuple(getattr(integration, "auth_domains", ()))
            if domains:
                removed = self.cookie_store.delete_domains(domains)
                self.cookie_store.mark_browser_clear([*domains, *removed])
        self.repository.set_task_status(task_id, "waiting_qr")
        success = await self.auth.login_with_qrcode(task_id, integration, should_stop)
        if success:
            self.repository.set_task_status(task_id, "parsing")
        return success

    async def resolve(self, task_id: int, url: str) -> None:
        self.repository.set_task_status(task_id, "parsing")
        should_stop = lambda: self.repository.should_stop(task_id)
        integration = self.integrations.match(url)
        discovery_url = integration.normalize(url) if integration is not None else url
        prepare_session = getattr(integration, "prepare_session", None)
        if callable(prepare_session):
            if inspect.iscoroutinefunction(prepare_session):
                await prepare_session()
            else:
                await asyncio.to_thread(prepare_session)
        auth_domains = tuple(getattr(integration, "auth_domains", ()))
        force_reauth = bool(auth_domains) and self.cookie_store.requires_reauth(
            auth_domains
        )
        authenticate_first = force_reauth or bool(
            getattr(integration, "authenticate_before_discovery", False)
        )
        disable_generic_fallback = bool(
            getattr(integration, "disable_generic_fallback", False)
        )
        visible_browser_auth_only = bool(
            getattr(integration, "visible_browser_auth_only", False)
        )
        authenticated = False
        login_attempted = False
        accepted_candidates: list[VideoCandidate] = []

        def remember(candidate: VideoCandidate) -> None:
            if not any(item.identity == candidate.identity for item in accepted_candidates):
                accepted_candidates.append(candidate)

        def add_specialized(candidate: VideoCandidate) -> None:
            self.repository.add_candidate(task_id, candidate)
            remember(candidate)

        def add_fallback(candidate: VideoCandidate) -> None:
            duplicate = fallback_duplicate_identity(
                candidate,
                accepted_candidates,
                engine=self.similarity_engine,
                threshold=self.config.fallback_jaccard_threshold,
            )
            aligned = replace(candidate, identity=duplicate) if duplicate else candidate
            self.repository.add_candidate(task_id, aligned)
            remember(aligned)

        async def run_specialized() -> DiscoveryOutcome | None:
            if integration is None:
                return None
            discover_async = getattr(integration, "discover_async", None)
            if callable(discover_async):
                return await discover_async(
                    discovery_url,
                    add_specialized,
                    should_stop,
                )
            return await asyncio.to_thread(
                integration.discover,
                discovery_url,
                add_specialized,
                should_stop,
            )

        async def run_generic() -> tuple[DiscoveryOutcome, list[VideoCandidate]]:
            candidates: list[VideoCandidate] = []

            def add_primary(candidate: VideoCandidate) -> None:
                candidates.append(candidate)
                add_fallback(candidate)

            result = await asyncio.to_thread(
                self.ytdlp.discover,
                discovery_url,
                add_primary,
                should_stop,
            )
            if result.is_collection and not should_stop():
                await self._enrich_candidates(
                    candidates,
                    source_page=url,
                    should_stop=should_stop,
                    add=add_fallback,
                )
            return result, candidates

        async def try_preferred_login(*, force: bool = False) -> bool:
            nonlocal authenticated, login_attempted
            if integration is None or (login_attempted and not force):
                return False
            if force:
                authenticated = False
            login_attempted = True
            success = await self._try_qrcode_login(
                task_id,
                integration,
                should_stop,
                skip_saved_login=force,
            )
            if success:
                authenticated = True
            return success

        try:
            outcome: DiscoveryOutcome | None = None
            integration_error = ""
            if authenticate_first:
                if await try_preferred_login():
                    force_reauth = False
                    authenticate_first = False
                else:
                    outcome = DiscoveryOutcome(0, False, True, "需要登录")

            if outcome is None and integration is not None:
                try:
                    outcome = await run_specialized()
                except IntegrationAuthRequired as exc:
                    integration_error = str(exc)
                    if await try_preferred_login(force=True):
                        outcome = await run_specialized()
                except IntegrationError as exc:
                    integration_error = str(exc)

            if outcome is None or (outcome.count == 0 and not authenticate_first):
                if disable_generic_fallback:
                    if outcome is None:
                        outcome = DiscoveryOutcome(
                            0,
                            False,
                            self._login_required(integration_error),
                            integration_error or "没有发现可转录的视频",
                        )
                else:
                    generic, _ = await run_generic()
                    if not generic.error and integration_error:
                        generic = DiscoveryOutcome(
                            generic.count,
                            generic.is_collection,
                            generic.needs_browser,
                            integration_error,
                        )
                    outcome = generic

            if (
                outcome.needs_browser
                and integration is not None
                and self._login_required(outcome.error)
                and not should_stop()
            ):
                if await try_preferred_login():
                    specialized = await run_specialized()
                    if specialized is not None and specialized.count:
                        outcome = specialized
                    else:
                        outcome, _ = await run_generic()

            visible_browser_disabled = bool(
                getattr(integration, "disable_visible_browser", False)
            )
            browser_is_allowed = not (
                visible_browser_auth_only and authenticated
            )
            if (
                outcome.needs_browser
                and not should_stop()
                and not visible_browser_disabled
                and browser_is_allowed
            ):
                self.repository.set_task_status(task_id, "browser")
                self.repository.reset_browser_action(task_id)
                retry_integration_error = ""

                def on_browser_ready() -> None:
                    self.repository.set_task_status(task_id, "waiting_browser")
                    self.auth.set_status(
                        task_id,
                        "waiting_browser",
                        "请在浏览器中完成登录",
                        platform=str(getattr(integration, "platform", "")),
                    )

                async def retry_primary_extractor() -> bool:
                    nonlocal retry_integration_error
                    try:
                        specialized = await run_specialized()
                    except IntegrationError as exc:
                        retry_integration_error = str(exc)
                        specialized = None
                    if specialized is not None and specialized.count > 0:
                        return not specialized.needs_browser
                    if disable_generic_fallback:
                        return False
                    retry, _ = await run_generic()
                    return retry.count > 0 and not retry.needs_browser

                check_login_method = getattr(integration, "check_login", None)
                verify_browser_login = bool(
                    getattr(integration, "verify_browser_login_automatically", True)
                )

                async def browser_check_login(
                    browser_cookies: list[dict[str, Any]],
                ) -> bool:
                    if not callable(check_login_method):
                        return False
                    cookies = {
                        str(item.get("name") or ""): str(item.get("value") or "")
                        for item in browser_cookies
                        if item.get("name") and item.get("value")
                    }
                    result = await asyncio.to_thread(check_login_method, cookies)
                    return getattr(result, "status", "") == "valid"

                def on_login_success() -> None:
                    nonlocal authenticated
                    authenticated = True
                    if auth_domains:
                        self.cookie_store.complete_reauth(auth_domains)
                    self.auth.set_status(
                        task_id,
                        "success",
                        "登录成功，正在继续解析",
                        platform=str(getattr(integration, "platform", "")),
                    )

                browser_result = await self.browser.discover(
                    url,
                    should_stop,
                    on_browser_ready=on_browser_ready,
                    get_user_action=lambda: self.repository.get_browser_action(task_id),
                    on_authenticated=retry_primary_extractor,
                    allow_scroll=(
                        not visible_browser_auth_only
                        and not self._candidate_link(discovery_url)
                    ),
                    check_login=(
                        browser_check_login
                        if callable(check_login_method) and verify_browser_login
                        else None
                    ),
                    on_login_success=on_login_success,
                )
                if should_stop():
                    return
                self.repository.set_task_status(task_id, "browser")
                if not visible_browser_auth_only:
                    for candidate in browser_result.candidates:
                        add_fallback(candidate)

                if (
                    visible_browser_auth_only
                    and authenticated
                    and not accepted_candidates
                ):
                    outcome = DiscoveryOutcome(
                        0,
                        False,
                        False,
                        retry_integration_error
                        or "登录成功，但未获取到视频信息",
                    )

                semaphore = asyncio.Semaphore(self.config.probe_concurrency)

                async def probe(link: str) -> None:
                    if should_stop():
                        return
                    if not (
                        self._candidate_link(link)
                        or await asyncio.to_thread(self.ytdlp.is_likely_supported, link)
                    ):
                        return
                    async with semaphore:
                        candidate = await asyncio.to_thread(self.ytdlp.probe, link, url)
                    if candidate is not None:
                        add_fallback(candidate)

                links = (
                    []
                    if visible_browser_auth_only
                    or self._candidate_link(discovery_url)
                    else browser_result.links
                )
                await asyncio.gather(*(probe(link) for link in links))

            if should_stop():
                return
            task = self.repository.get_task(task_id)
            if task and task["item_count"]:
                self.repository.set_task_status(task_id, "ready")
            else:
                message = outcome.error or "没有发现可转录的视频"
                self.repository.set_task_status(task_id, "failed", message)
        except Exception as exc:
            if should_stop():
                return
            task = self.repository.get_task(task_id)
            if task and task["item_count"]:
                self.repository.set_task_status(task_id, "ready", str(exc))
            else:
                self.repository.set_task_status(task_id, "failed", str(exc))
