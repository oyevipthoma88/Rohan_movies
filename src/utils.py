from __future__ import annotations

import re
import time
import datetime
import asyncio
from typing import Optional

import aiohttp
from rapidfuzz import fuzz

from config import (
    MOVIE_CHANNEL, OPENAI_API_KEY, OPENAI_BASE_URL,
    FIRST_DAY_FREE, DAILY_FREE, REFERRAL_REWARDS,
)
from db import db

QUALITY_PATTERNS = [
    (r"\b(2160p|4k|uhd)\b", "2160p"),
    (r"\b(1440p)\b", "1440p"),
    (r"\b(1080p|fhd|full\s?hd)\b", "1080p"),
    (r"\b(720p|hd)\b", "720p"),
    (r"\b(480p|sd)\b", "480p"),
    (r"\b(360p)\b", "360p"),
    (r"\b(cam|camrip|hdcam|predvd)\b", "CAM"),
    (r"\b(hdts|ts)\b", "HDTS"),
    (r"\b(web-?dl|webrip|web)\b", "WEB-DL"),
    (r"\b(bluray|brrip|bdrip)\b", "BluRay"),
    (r"\b(hdrip)\b", "HDRip"),
]

LANG_PATTERNS = [
    "hindi", "english", "tamil", "telugu", "malayalam", "kannada", "punjabi",
    "marathi", "bengali", "gujarati", "korean", "japanese", "dual audio", "multi audio",
]

JUNK = [
    r"@\w+", r"https?://\S+", r"\bwww\.\S+", r"\b(x264|x265|hevc|avc|aac|ddp?5\.1|10bit|esub[s]?)\b",
    r"\b(download|movie|full|watch|link|join|telegram|channel)\b",
    r"[\[\]\(\)\{\}_|•·—–~*#]",
]

SIZE_RE = re.compile(r"\b(\d+(?:\.\d+)?\s?(?:gb|mb))\b", re.I)
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


def parse_caption(caption: str) -> dict:
    """Har channel ka caption format alag hota hai -> normalize karke title/quality nikaalo."""
    raw = (caption or "").strip()
    text = raw.replace("\n", " ")
    low = text.lower()

    quality = ""
    for pat, label in QUALITY_PATTERNS:
        if re.search(pat, low):
            quality = label
            break
    # resolution + source dono mile to jodo
    res = next((l for p, l in QUALITY_PATTERNS[:6] if re.search(p, low)), "")
    src = next((l for p, l in QUALITY_PATTERNS[6:] if re.search(p, low)), "")
    quality = " ".join(x for x in [res, src] if x) or quality or "Unknown"

    language = ", ".join(l.title() for l in LANG_PATTERNS if l in low)
    size_m = SIZE_RE.search(text)
    size = size_m.group(1).upper() if size_m else ""
    year_m = YEAR_RE.search(text)
    year = year_m.group(1) if year_m else ""

    title = text
    if year:
        title = title.split(year)[0]
    title = re.sub(r"\b(s\d{1,2}\s?e\d{1,2})\b.*", "", title, flags=re.I)
    for pat, _ in QUALITY_PATTERNS:
        title = re.sub(pat, " ", title, flags=re.I)
    for j in JUNK:
        title = re.sub(j, " ", title, flags=re.I)
    title = SIZE_RE.sub(" ", title)
    for lang in LANG_PATTERNS:
        title = re.sub(rf"\b{lang}\b", " ", title, flags=re.I)
    title = re.sub(r"[^\w\s&':.,-]", " ", title, flags=re.UNICODE)
    title = re.sub(r"[\u2600-\u27BF\U0001F000-\U0001FAFF]", " ", title)
    title = re.sub(r"[._\-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -:|")
    if not title:
        title = raw[:60] or "Unknown"
    return {
        "title": title.title(),
        "year": year,
        "quality": quality,
        "language": language,
        "size": size,
    }


def extract_caption_extra(caption: str, meta: dict) -> str:
    """Keep useful original caption lines that are not standard catalog metadata."""
    standard = re.compile(
        r"(movie|title|quality|resolution|duration|runtime|size|language|audio|join|visit|enjoy|download|watch|telegram|channel)",
        re.I,
    )
    title_norm = norm(meta.get("title", ""))
    keep = []
    for raw_line in (caption or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cleaned = re.sub(r"[🎬🎥⏱️📢🌐⚡🍿]", "", line).strip(" :|-_")
        line_norm = norm(cleaned)
        if not line_norm or (title_norm and line_norm == title_norm):
            continue
        metadata_markers = [meta.get("year", ""), meta.get("quality", ""), meta.get("language", ""), meta.get("size", "")]
        if title_norm and title_norm in line_norm and any(marker and marker.lower() in cleaned.lower() for marker in metadata_markers):
            continue
        if standard.search(cleaned) and any(token in cleaned.lower() for token in ("movie", "quality", "duration", "join", "visit", "download")):
            continue
        if line not in keep:
            keep.append(line)
    return "\n".join(keep)[:700]


def norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


async def get_movie_channel():
    """Owner panel se set kiya channel > env MOVIE_CHANNEL."""
    v = (await db.get("movie_channel", "")).strip()
    if v:
        try:
            return int(v)
        except ValueError:
            return v if v.startswith("@") else "@" + v
    return MOVIE_CHANNEL


async def search_movies(query: str, limit: int = 20, content_type: str = "") -> list:
    """Return exact/strong title matches; avoid broad fuzzy matches for short queries."""
    # Only records linked to a MAIN_DATA post are deliverable. This prevents
    # stale source-channel rows from appearing and then failing on tap.
    q_norm = norm(query)
    if not q_norm:
        return []

    # For large catalogs, use SQL LIKE pre-filter to avoid loading 1M+ rows.
    total_count = await db.movie_count(storage_only=True)
    if total_count > 5000:
        # Try SQL pre-filter with the raw query first
        candidates = await db.search_movies_sql(query, content_type=content_type, limit=limit)
        if not candidates:
            # Fallback: try with normalized query tokens
            first_token = q_norm.split()[0] if q_norm.split() else q_norm
            candidates = await db.search_movies_sql(first_token, content_type=content_type, limit=limit)
        if not candidates:
            return []
        movies = candidates
    else:
        movies = await db.all_movies(storage_only=True)
        if content_type in {"movie", "series", "anime"}:
            movies = [movie for movie in movies if (movie["content_type"] or "movie") == content_type]
        if not movies:
            return []

    by_id = {m["msg_id"]: m for m in movies}
    scored = []
    exact = []
    for msg_id, movie in by_id.items():
        title = norm(movie["title"])
        title_tokens = set(title.split())
        year = norm(movie["year"] or "")
        name = f"{title} {year}".strip()
        if q_norm in title_tokens or title.startswith(q_norm) or q_norm == title:
            exact.append((100 if q_norm == title else 98, msg_id))
            continue
        token_score = max((fuzz.ratio(q_norm, token) for token in title_tokens), default=0)
        score = max(fuzz.WRatio(q_norm, name), fuzz.token_set_ratio(q_norm, name), fuzz.ratio(q_norm, title), token_score)
        threshold = 78 if len(q_norm) >= 4 else 92
        if score >= threshold:
            scored.append((score, msg_id))

    if exact:
        exact.sort(key=lambda item: (-item[0], -int(by_id[item[1]]["msg_id"])))
        return [by_id[i] for _, i in exact[:limit]]
    if not scored:
        return []
    scored.sort(reverse=True)
    best = scored[0][0]
    return [by_id[i] for score, i in scored if score >= max(82, best - 6)][:limit]


# ---------------- quota ----------------
def today() -> str:
    return datetime.date.today().isoformat()


def is_first_calendar_day(user) -> bool:
    joined_at = int(user["joined_at"] or 0) if user else 0
    return bool(joined_at) and datetime.date.fromtimestamp(joined_at).isoformat() == today()


def is_premium(user) -> bool:
    return bool(user) and user["premium_until"] > int(time.time())


_QUOTA_LOCKS: dict[int, asyncio.Lock] = {}


def _quota_lock(user_id: int) -> asyncio.Lock:
    return _QUOTA_LOCKS.setdefault(int(user_id), asyncio.Lock())


async def check_quota(user_id: int) -> tuple[bool, str]:
    user = await db.user(user_id)
    if not user:
        return False, "User not found, /start dabao."
    if is_premium(user):
        return True, ""
    d = today()
    used = user["free_used_today"] if user["last_free_day"] == d else 0
    limit = FIRST_DAY_FREE if is_first_calendar_day(user) else DAILY_FREE
    if used >= limit:
        return False, (
            f"❌ Aaj ka free limit ({limit}) khatam ho gaya.\n"
            "Kal dobara 1 free search milegi, ya abhi Premium le lo 👇"
        )
    return True, ""


async def consume_quota(user_id: int) -> None:
    await reserve_quota(user_id)


async def reserve_quota(user_id: int) -> tuple[bool, str]:
    """Atomically reserve one free delivery slot for a user."""
    async with _quota_lock(user_id):
        user = await db.user(user_id)
        if not user:
            return False, "User not found, /start dabao."
        if is_premium(user):
            return True, ""
        d = today()
        used = user["free_used_today"] if user["last_free_day"] == d else 0
        limit = FIRST_DAY_FREE if is_first_calendar_day(user) else DAILY_FREE
        if used >= limit:
            return False, f"❌ Aaj ka free limit ({limit}) khatam ho gaya."
        used += 1
        fields = {"free_used_today": used, "last_free_day": d}
        if is_first_calendar_day(user) and used >= limit:
            fields["first_bonus_used"] = 1
        await db.update_user(user_id, **fields)
        return True, ""


async def refund_quota(user_id: int) -> None:
    """Return a reserved slot when delivery fails before reaching the user."""
    async with _quota_lock(user_id):
        user = await db.user(user_id)
        if not user or is_premium(user):
            return
        d = today()
        if user["last_free_day"] != d or user["free_used_today"] <= 0:
            return
        used = user["free_used_today"] - 1
        await db.update_user(
            user_id,
            free_used_today=used,
            last_free_day=d,
            first_bonus_used=0 if used < FIRST_DAY_FREE else user["first_bonus_used"],
        )


async def apply_referral_rewards(referrer_id: int) -> Optional[str]:
    user = await db.user(referrer_id)
    if not user:
        return None
    count = user["referrals"]
    rewarded = user["rewarded_upto"]
    granted = None
    for need in sorted(REFERRAL_REWARDS):
        if count >= need > rewarded:
            hours = REFERRAL_REWARDS[need]
            await db.add_premium(referrer_id, hours / 24)
            await db.update_user(referrer_id, rewarded_upto=need)
            granted = f"🎉 {need} referrals complete! {hours} hour premium mil gaya."
    return granted


async def transcribe_voice(path: str) -> Optional[str]:
    """Voice search: whisper-compatible API se text nikaalo."""
    if not OPENAI_API_KEY:
        return None
    try:
        with open(path, "rb") as f:
            data = aiohttp.FormData()
            data.add_field("file", f, filename="voice.ogg", content_type="audio/ogg")
            data.add_field("model", "whisper-1")
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{OPENAI_BASE_URL}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as r:
                    if r.status != 200:
                        return None
                    out = await r.json()
        return (out.get("text") or "").strip() or None
    except Exception:
        return None


def human_left(ts: int) -> str:
    left = ts - int(time.time())
    if left <= 0:
        return "expired"
    days, rem = divmod(left, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 3000:
        return "Lifetime"
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins and not days:
        parts.append(f"{mins}m")
    return " ".join(parts) or "few seconds"


# ---------------- channel helpers ----------------
_CHAT_CACHE: dict[str, dict] = {}


async def resolve_channel(bot) -> Optional[dict]:
    """Movie channel ki info (id/username/title) — cache ke saath."""
    ch = await get_movie_channel()
    if not ch:
        return None
    key = str(ch)
    if key in _CHAT_CACHE:
        return _CHAT_CACHE[key]
    try:
        chat = await bot.get_chat(ch)
    except Exception:
        return {"id": ch, "username": "", "title": str(ch), "protected": None}
    info = {
        "id": chat.id,
        "username": chat.username or "",
        "title": chat.title or str(ch),
        "protected": bool(getattr(chat, "has_protected_content", False)),
    }
    _CHAT_CACHE[key] = info
    return info


def clear_channel_cache() -> None:
    _CHAT_CACHE.clear()


def post_link(info: Optional[dict], msg_id: int) -> str:
    """Channel post ka direct link — public channel ya private (t.me/c/...)."""
    if not info:
        return ""
    if info.get("username"):
        return f"https://t.me/{info['username']}/{msg_id}"
    cid = str(info.get("id", ""))
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{msg_id}"
    return ""


def user_tag(u) -> str:
    """Log ke liye poora user detail."""
    if not u:
        return "unknown"
    uname = f"@{u.username}" if getattr(u, "username", None) else "no-username"
    return f"{getattr(u, 'full_name', '') or '-'} ({uname}) <code>{u.id}</code>"
