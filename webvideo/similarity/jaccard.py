from __future__ import annotations

import re
import unicodedata


PLATFORM_SUFFIX_RE = re.compile(
    r"(?:\s*[-·|]\s*(?:抖音|小红书|快手|哔哩哔哩|bilibili|douyin|"
    r"xiaohongshu|kuaishou))+$",
    re.I,
)
MEDIA_SUFFIX_RE = re.compile(
    r"\s*[·|]\s*(?:media-(?:video|audio)[^\s]*|[^\s]+\.(?:m4a|mp3|mp4|"
    r"mov|webm|m3u8|mpd))$",
    re.I,
)


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = MEDIA_SUFFIX_RE.sub("", text)
    text = PLATFORM_SUFFIX_RE.sub("", text)
    return "".join(character for character in text if character.isalnum())


def _character_shingles(value: str) -> set[str]:
    normalized = normalize_title(value)
    if not normalized:
        return set()
    if len(normalized) < 4:
        return {normalized}
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


class JaccardTitleSimilarity:
    """Language-neutral Jaccard score over normalized Unicode bigrams."""

    def score(self, left: str, right: str) -> float:
        left_tokens = _character_shingles(left)
        right_tokens = _character_shingles(right)
        if not left_tokens or not right_tokens:
            return 0.0
        union = left_tokens | right_tokens
        return len(left_tokens & right_tokens) / len(union) if union else 0.0
