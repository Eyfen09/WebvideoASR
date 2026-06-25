from __future__ import annotations

from typing import Protocol


class SimilarityEngine(Protocol):
    """Replaceable text-similarity boundary used by fallback de-duplication."""

    def score(self, left: str, right: str) -> float: ...
