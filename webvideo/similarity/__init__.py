from .base import SimilarityEngine
from .jaccard import JaccardTitleSimilarity, normalize_title
from .matcher import fallback_duplicate_identity

__all__ = [
    "JaccardTitleSimilarity",
    "SimilarityEngine",
    "fallback_duplicate_identity",
    "normalize_title",
]
