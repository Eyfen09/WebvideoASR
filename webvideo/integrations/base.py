from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from webvideo.models import DiscoveryOutcome, VideoCandidate


@dataclass(frozen=True)
class QRChallenge:
    key: str
    url: str
    image_data_url: str = ""


@dataclass(frozen=True)
class QRPollResult:
    status: str
    message: str
    cookies: dict[str, str] = field(default_factory=dict)
    cookie_domain: str = ""
    expires: int = 0
    username: str = ""


@dataclass(frozen=True)
class LoginCheck:
    status: str
    username: str = ""


class QRAuthProvider(Protocol):
    platform: str

    def create_qrcode(self) -> QRChallenge: ...

    def poll_qrcode(self, challenge: QRChallenge) -> QRPollResult: ...


class SiteIntegration(Protocol):
    name: str

    def matches(self, url: str) -> bool: ...

    def normalize(self, url: str) -> str: ...

    def discover(
        self,
        url: str,
        on_candidate: Callable[[VideoCandidate], None],
        should_stop: Callable[[], bool],
    ) -> DiscoveryOutcome | None: ...


class IntegrationError(RuntimeError):
    pass


class IntegrationAuthRequired(IntegrationError):
    pass
