"""Reliable historical importer and delivery for existing Telegram channel posts."""
from __future__ import annotations

import asyncio
import logging
import time

from config import MAIN_DATA, SCAN_COMMIT_BATCH, TG_API_HASH, TG_API_ID, TG_SESSION
from db import db
from movie_catalog import detect_content_type
from utils import parse_caption

_mtproto_client = None


async def _get_client():
    global _mtproto_client
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        return None
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    if _mtproto_client is None:
        _mtproto_client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)
    if not _mtproto_client.is_connected():
        await _mtproto_client.connect()
    if not await _mtproto_client.is_user_authorized():
        logging.error("TG_SESSION is not authorized; generate a new StringSession")
        return None
    return _mtproto_client


class ImportStats:
    def __init__(self) -> None:
        self.checked = 0
        self.indexed = 0
        self.skipped = 0
        self.errors = 0
        self.last_error = ""


def _format_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return ""
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


async def deliver_main_data_to_user(user_id: int, message_id: int, caption: str = "") -> str:
    """Deliver an original MAIN_DATA post through the configured user session."""
    client = await _get_client()
    if client is None:
        return ""
    try:
        if not caption:
            await client.forward_messages(user_id, message_id, from_peer=MAIN_DATA)
            return "mtproto/forward"
        message = await client.get_messages(MAIN_DATA, ids=message_id)
        if not message or not message.media:
            return ""
        await client.send_file(user_id, message.media, caption=caption, parse_mode="html")
        return "mtproto/send_file_captioned"
    except Exception:
        message = await client.get_messages(MAIN_DATA, ids=message_id)
        if not message or not message.media:
            return ""
        await client.send_file(user_id, message.media, caption=message.message or "")
        return "mtproto/send_file"


async def enumerate_message_ids(channel, min_id: int = 0, max_id: int = 0) -> list[int]:
    """Enumerate real historical message IDs from a Telegram channel."""
    client = await _get_client()
    if client is None:
        return []
    entity = await client.get_entity(channel)
    return [
        message.id
        async for message in client.iter_messages(
            entity, min_id=min_id, max_id=max_id, limit=None
        )
        if getattr(message, "id", None) and getattr(message, "media", None)
    ]


async def find_duplicate_caption_ids(channel=MAIN_DATA, min_id: int = 0, max_id: int = 0) -> tuple[int, int, list[int]]:
    """Return (checked, skipped, older duplicate IDs) from real channel history."""
    client = await _get_client()
    if client is None:
        raise RuntimeError("TG_SESSION is not configured or authorized")
    entity = await client.get_entity(channel)
    seen = set()
    duplicate_ids = []
    checked = skipped = 0
    async for message in client.iter_messages(entity, min_id=min_id, max_id=max_id, limit=None):
        media = getattr(message, "media", None)
        caption = (getattr(message, "message", "") or "").strip()
        if not media or not caption:
            skipped += 1
            continue
        checked += 1
        key = " ".join(caption.split()).casefold()
        if key in seen:
            duplicate_ids.append(int(message.id))
        else:
            seen.add(key)
    return checked, skipped, duplicate_ids


async def import_main_data(progress=None, min_id: int = 0, max_id: int = 0,
                           resume_key: str = "", attempts: int = 3) -> ImportStats:
    """Import every existing media post from MAIN_DATA without copying files.

    Fast path: MTProto history read (no forwarding), batched SQLite commits and
    a durable resume cursor so an interrupted or timed-out run continues from
    where it stopped instead of rescanning the whole channel.
    """
    stats = ImportStats()
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        stats.last_error = "TG_API_ID/TG_API_HASH/TG_SESSION not configured"
        return stats
    client = await _get_client()
    if client is None:
        stats.last_error = "TG_SESSION is not authorized"
        return stats

    cursor_max = max_id
    if resume_key:
        saved = await db.get(resume_key, "")
        if saved and saved.isdigit() and int(saved) > 0:
            # Newest→oldest walk: resume below the last processed message ID.
            cursor_max = int(saved)

    started = time.monotonic()
    pending: list[tuple] = []
    last_id = 0

    async def flush(cursor_id: int) -> None:
        """One bulk write + durable cursor, so a reboot never restarts at 0."""
        nonlocal pending
        if pending:
            stats.indexed += await db.bulk_upsert_movies(pending)
            pending = []
        await db.commit()
        if resume_key and cursor_id:
            await db.set(resume_key, str(cursor_id))

    for attempt in range(max(1, attempts)):
        try:
            async for message in client.iter_messages(
                MAIN_DATA, min_id=min_id, max_id=cursor_max, limit=None, wait_time=2, batch_size=1000,
            ):
                last_id = int(getattr(message, "id", 0) or 0)
                stats.checked += 1
                try:
                    document = getattr(message, "document", None)
                    if not message.media or not document:
                        stats.skipped += 1
                    else:
                        mime = getattr(document, "mime_type", "") or ""
                        is_video = mime.startswith("video/")
                        is_audio = mime.startswith("audio/")
                        if not (is_video or is_audio or mime):
                            stats.skipped += 1
                        else:
                            caption = message.message or ""
                            attributes = getattr(document, "attributes", []) or []
                            file_name = next((getattr(a, "file_name", "") for a in attributes if getattr(a, "file_name", "")), "")
                            duration = next((getattr(a, "duration", 0) for a in attributes if getattr(a, "duration", 0)), 0)
                            meta = parse_caption(caption or file_name)
                            meta["duration"] = _format_duration(duration)
                            if not meta["size"]:
                                size_mb = (getattr(document, "size", 0) or 0) / 1048576
                                meta["size"] = f"{size_mb / 1024:.1f}GB" if size_mb > 1024 else f"{size_mb:.0f}MB"
                            access_hash = getattr(document, "access_hash", 0) or 0
                            file_key = f"tg:{document.id}:{access_hash}"
                            file_type = "video" if is_video else "audio" if is_audio else "document"
                            pending.append((
                                message.id, meta["title"], meta["year"], meta["quality"],
                                meta["language"], meta["size"], meta["duration"], caption,
                                file_key, file_type, message.id,
                                detect_content_type(caption), int(time.time()),
                            ))
                except Exception as exc:
                    stats.errors += 1
                    stats.last_error = str(exc)
                    logging.exception("MTProto import failed for MAIN_DATA message %s", getattr(message, "id", "?"))
                if len(pending) >= SCAN_COMMIT_BATCH:
                    await flush(last_id)
                if progress and (stats.checked % 1000 == 0 or time.monotonic() - started >= 20):
                    await progress(stats)
                    started = time.monotonic()
            break
        except asyncio.CancelledError:
            await flush(last_id)
            raise
        except Exception as exc:
            await flush(last_id)
            stats.errors += 1
            stats.last_error = str(exc)
            logging.warning("MAIN_DATA import attempt %s failed: %s", attempt + 1, exc)
            if attempt + 1 >= max(1, attempts):
                break
            wait_for = getattr(exc, "seconds", 0) or min(60, 3 * (attempt + 1))
            await asyncio.sleep(wait_for)
            if last_id:
                cursor_max = last_id
            client = await _get_client()
            if client is None:
                stats.last_error = "TG_SESSION is not authorized"
                break

    await flush(last_id)
    if resume_key:
        # A finished pass has no remainder; clear the cursor so the next manual
        # run rescans the full channel from the newest post.
        await db.set(resume_key, "0" if not stats.last_error else str(last_id or 0))
    if progress:
        await progress(stats)
    return stats


async def close_mtproto() -> None:
    global _mtproto_client
    if _mtproto_client is not None:
        await _mtproto_client.disconnect()
    _mtproto_client = None


__all__ = ["ImportStats", "deliver_main_data_to_user", "enumerate_message_ids", "find_duplicate_caption_ids", "import_main_data", "close_mtproto"]

