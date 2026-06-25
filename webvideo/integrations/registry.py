from __future__ import annotations

from collections.abc import Iterable

from webvideo.auth import CookieStore
from webvideo.config import WebVideoConfig

from .base import SiteIntegration
from .bilibili import BilibiliIntegration
from .douyin import DouyinIntegration
from .kuaishou import KuaishouIntegration
from .xiaohongshu import XiaohongshuIntegration
from .youtube import YoutubeIntegration


class IntegrationRegistry:
    def __init__(self, integrations: Iterable[SiteIntegration]) -> None:
        self.integrations = tuple(integrations)

    def match(self, url: str) -> SiteIntegration | None:
        return next(
            (integration for integration in self.integrations if integration.matches(url)),
            None,
        )


def default_registry(
    config: WebVideoConfig,
    cookie_store: CookieStore,
) -> IntegrationRegistry:
    return IntegrationRegistry(
        [
            BilibiliIntegration(
                cookie_store,
                timeout=config.request_timeout_seconds,
            ),
            DouyinIntegration(config, cookie_store),
            XiaohongshuIntegration(config, cookie_store),
            KuaishouIntegration(config, cookie_store),
            YoutubeIntegration(config, cookie_store),
        ]
    )
