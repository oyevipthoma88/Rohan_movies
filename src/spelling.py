"""Lightweight spelling correction for movie name searches.
Uses rapidfuzz for fuzzy matching against the known catalog titles."""
import re
from typing import Optional

from rapidfuzz import fuzz, process


def _normalize(s: str) -> str:
    s = re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


# Common misspelling → correction map for Indian movie names
COMMON_FIXES = {
    "mirjapur": "mirzapur",
    "mirjapurr": "mirzapur",
    "mirzapurr": "mirzapur",
    "hanumanansh": "hanumanansh",
    "hanuman ansh": "hanumanansh",
    "hanumanash": "hanumanansh",
    "sooryavanshi": "sooryavanshi",
    "suryavanshi": "sooryavanshi",
    "suryavansh": "sooryavanshi",
    "brahmastra": "brahmastra",
    "brahmashtra": "brahmastra",
    "brahmaastra": "brahmastra",
    "ranjana": "ranjana",
    "ranjnaa": "ranjana",
    "krissh": "krrish",
    "krishh": "krrish",
    "krisshh": "krrish",
    "rajneeti": "raajneeti",
    "raajneeti": "raajneeti",
    "agneepath": "agneepath",
    "agneepaath": "agneepath",
    "donnd": "don",
    "sholayy": "sholay",
    "sholaay": "sholay",
}


def correct_spelling(query: str, catalog_titles: list[str] = None) -> Optional[str]:
    """Try to correct common spelling mistakes in movie names.

    First checks a manual common-fixes map, then uses fuzzy matching
    against known catalog titles to find the closest correct spelling.

    Returns the corrected query, or None if no confident correction found.
    """
    q = (query or "").strip()
    if not q or len(q) < 3:
        return None

    q_norm = _normalize(q)
    if not q_norm:
        return None

    # 1) Check common fixes map (exact normalized match)
    if q_norm in COMMON_FIXES:
        return COMMON_FIXES[q_norm]

    # 2) Fuzzy match against catalog titles if provided
    if catalog_titles:
        norm_titles = [(t, _normalize(t)) for t in catalog_titles if t]
        choices = {i: nt for i, (_, nt) in enumerate(norm_titles) if nt}
        if not choices:
            return None
        result = process.extractOne(
            q_norm, choices, scorer=fuzz.WRatio, score_cutoff=80
        )
        if result:
            idx, score, _ = result
            if score >= 80:
                original_title = norm_titles[idx][0]
                return original_title

    return None


def suggest_corrections(query: str, catalog_titles: list[str], limit: int = 3) -> list[str]:
    """Return up to `limit` suggested corrections for a misspelled query."""
    q_norm = _normalize(query)
    if not q_norm or not catalog_titles:
        return []
    norm_titles = [(t, _normalize(t)) for t in catalog_titles if t]
    choices = {i: nt for i, (_, nt) in enumerate(norm_titles) if nt}
    if not choices:
        return []
    results = process.extract(
        q_norm, choices, scorer=fuzz.WRatio, score_cutoff=70, limit=limit
    )
    suggestions = []
    for idx, score, _ in results:
        suggestions.append(norm_titles[idx][0])
    return suggestions
