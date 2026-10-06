import re


CATALOG_TYPES = {"movie", "series", "anime"}

_ANIME_RE = re.compile(r"\b(anime|donghua|animation series)\b", re.IGNORECASE)
_SERIES_RE = re.compile(
    r"\b(web[ -]?series|tv[ -]?series|season\s*\d+|episode\s*\d+|complete\s+series|s\d{1,2}\s*e\d{1,2})\b",
    re.IGNORECASE,
)


def detect_content_type(text: str) -> str:
    """Classify indexed media conservatively from its existing caption metadata."""
    value = text or ""
    if _ANIME_RE.search(value):
        return "anime"
    if _SERIES_RE.search(value):
        return "series"
    return "movie"


def catalog_label(content_type: str) -> str:
    return {"movie": "Movies", "series": "Web Series", "anime": "Anime"}.get(content_type, "Movies")
