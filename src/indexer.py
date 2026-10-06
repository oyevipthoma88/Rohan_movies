import asyncio
import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.types import Message

from db import db
from movie_catalog import detect_content_type
from config import LOG_CHANNEL, OWNER_IDS, STORAGE_CHANNEL, MAIN_DATA
from transfer_manager import transfer_manager
from snapshot import SNAPSHOT_MARKER, request_snapshot
from keyboards import source_approval_kb
from utils import get_movie_channel, parse_caption
from log_helper import log_new_movie_added, log_event

router = Router()

# Telegram error classification (lowercase substrings)
MISSING_HINTS = (
    "message to forward not found",
    "message to be forwarded not found",
    "message to copy not found",
    "message_id_invalid",
    "message id invalid",
    "message to forward is empty",
)
PROTECTED_HINTS = (
    "message can't be forwarded",
    "message can't be copied",
    "can't be forwarded",
    "can't be copied",
    "forwarding of the message",
    "chat_forwards_restricted",
    "restricted",
)
FATAL_HINTS = (
    "chat not found",
    "bot is not a member",
    "not enough rights",
    "chat_admin_required",
    "bot was kicked",
    "chat_write_forbidden",
)
RETRY_RE = re.compile(r"retry after (\d+)", re.I)


def classify(err: str) -> str:
    low = err.lower()
    if any(h in low for h in MISSING_HINTS):
        return "missing"
    if any(h in low for h in PROTECTED_HINTS):
        return "protected"
    if any(h in low for h in FATAL_HINTS):
        return "fatal"
    if "too many requests" in low or "flood" in low:
        return "flood"
    return "error"


def format_duration(seconds: int | float | None) -> str:
    total = max(0, int(seconds or 0))
    if not total:
        return "Unknown"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def build_meta(media, caption: str) -> dict:
    meta = parse_caption(caption or (getattr(media, "file_name", "") or ""))
    if not meta["size"]:
        size_mb = (getattr(media, "file_size", 0) or 0) / 1048576
        meta["size"] = f"{size_mb/1024:.1f}GB" if size_mb > 1024 else f"{size_mb:.0f}MB"
    meta["duration"] = format_duration(getattr(media, "duration", 0))
    return meta


def media_kind(message) -> str:
    if message.video:
        return "video"
    if message.document:
        return "document"
    if message.audio:
        return "audio"
    return ""


async def _is_movie_channel(chat) -> bool:
    primary = await get_movie_channel()
    raw = await db.get("movie_channels", str(primary or ""))
    targets = [x.strip() for x in raw.split(",") if x.strip()] or ([str(primary)] if primary else [])
    for target in targets:
        if target.lstrip("-").isdigit() and chat.id == int(target):
            return True
        if (chat.username or "").lower() == target.lstrip("@").lower():
            return True
    return False


async def index_message(message: Message, bot: Bot | None = None, storage_channel: int = 0) -> bool:
    """Index media; optionally copy admin-submitted media into permanent storage."""
    if (message.caption or "").strip() == SNAPSHOT_MARKER:
        return False
    media = message.video or message.document or message.audio
    if not media:
        return False
    index_id = message.message_id
    storage_msg_id = 0
    copied_message_id = 0
    caption = message.caption or message.text or ""
    if storage_channel and message.chat.id != storage_channel:
        if await db.has_duplicate_movie(getattr(media, "file_id", ""), caption):
            return False
    if storage_channel and message.chat.id == storage_channel:
        storage_msg_id = message.message_id
    elif bot and storage_channel:
        saved = await bot.forward_message(storage_channel, message.chat.id, message.message_id, disable_notification=True)
        copied_message_id = saved.message_id
        index_id = saved.message_id
        storage_msg_id = saved.message_id
        media = message.video or message.document or message.audio
    meta = build_meta(media, caption)
    inserted = await db.upsert_movie(index_id, meta["title"], meta["year"], meta["quality"],
                                      meta["language"], meta["size"], caption,
                                      media.file_id, media_kind(message), detect_content_type(caption),
                                      duration=meta["duration"], storage_msg_id=storage_msg_id)
    if not inserted and copied_message_id and bot and storage_channel:
        # Do not leave an orphan duplicate in MAIN_DATA when a source channel
        # contains a file that is already present in the canonical catalog.
        try:
            await bot.delete_message(storage_channel, copied_message_id)
        except Exception as exc:
            logging.warning("Could not remove duplicate MAIN_DATA copy %s: %s", copied_message_id, exc)
    return inserted


def _approval_key(source_chat_id: int, source_message_id: int) -> str:
    return f"source_approval:{source_chat_id}:{source_message_id}"


async def _send_approval_request(message: Message, bot: Bot) -> None:
    media = message.video or message.document or message.audio
    if not media:
        return
    meta = build_meta(media, message.caption or "")
    title = meta["title"] or "Unknown title"
    caption = (
        "🆕 <b>New source movie upload</b>\n\n"
        f"Title: <b>{title}</b>\n"
        f"Quality: <code>{meta['quality'] or 'Unknown'}</code>\n"
        f"Source: <code>{message.chat.id}</code> / post <code>{message.message_id}</code>\n\n"
        "Approve karoge to ye movie MAIN_DATA me copy/index ho jayegi."
    )
    markup = source_approval_kb(message.chat.id, message.message_id)
    targets = list(dict.fromkeys([*OWNER_IDS, *([LOG_CHANNEL] if LOG_CHANNEL else [])]))
    for target in targets:
        try:
            await bot.copy_message(
                target,
                message.chat.id,
                message.message_id,
                caption=caption[:1024],
                reply_markup=markup,
            )
        except Exception as exc:
            logging.warning("Source approval request failed for target %s: %s", target, exc)
            try:
                await bot.send_message(target, caption, reply_markup=markup)
            except Exception as fallback_exc:
                logging.warning("Source approval text fallback failed for target %s: %s", target, fallback_exc)


@router.channel_post()
async def index_new(message: Message, bot: Bot):
    """Naye source post par instant auto-transfer via helper bots.
    No manual approval needed — helper bots forward/copy to MAIN_DATA immediately."""
    if not await _is_movie_channel(message.chat):
        return
    media = message.video or message.document or message.audio
    if not media:
        return
    # Skip if already in MAIN_DATA (dedup by file_id or caption)
    caption = message.caption or message.text or ""
    if await db.has_duplicate_movie(getattr(media, "file_id", ""), caption):
        logging.info("Source msg %s already in catalog, skipping", message.message_id)
        return

    # Check if auto-transfer is enabled (default: yes)
    auto_transfer = await db.get("auto_transfer_enabled", "1")
    if auto_transfer == "0":
        # Fallback to manual approval mode
        await db.set(_approval_key(message.chat.id, message.message_id), "pending")
        await _send_approval_request(message, bot)
        logging.info("Auto-transfer disabled, approval requested for source msg %s", message.message_id)
        return

    # Instant transfer via helper bots
    src_chat = message.chat.id
    src_msg_id = message.message_id
    logging.info("Auto-transfer: source msg %s from %s -> MAIN_DATA", src_msg_id, src_chat)

    result = await transfer_manager.transfer_message(src_chat, src_msg_id, MAIN_DATA)

    if result.success and result.dest_msg_id:
        # Index the transferred message in MAIN_DATA — NO movie forward to log channel.
        try:
            # Build metadata from the original source message
            meta = build_meta(media, caption)
            inserted = await db.upsert_movie(
                result.dest_msg_id, meta["title"], meta["year"], meta["quality"],
                meta["language"], meta["size"], caption,
                getattr(media, "file_id", ""), media_kind(message), detect_content_type(caption),
                duration=meta["duration"], storage_msg_id=result.dest_msg_id
            )
            if inserted:
                await request_snapshot(bot)
                logging.info("Auto-transfer OK: '%s' -> MAIN_DATA msg %s via %s",
                             meta["title"], result.dest_msg_id, result.method)
                await log_new_movie_added(bot, meta['title'], meta['quality'], src_chat, result.dest_msg_id, f"{result.method} via @{result.bot_username}", await db.movie_count(storage_only=True))
            else:
                logging.info("Auto-transfer: duplicate, not indexed")
                # Delete the duplicate from MAIN_DATA
                try:
                    await bot.delete_message(MAIN_DATA, result.dest_msg_id)
                except Exception:
                    pass
        except Exception as exc:
            logging.exception("Auto-transfer indexing failed for %s: %s", result.dest_msg_id, exc)
    else:
        logging.error("Auto-transfer FAILED: source msg %s: %s", src_msg_id, result.error)
        # Fallback to manual approval
        await db.set(_approval_key(src_chat, src_msg_id), "pending")
        await _send_approval_request(message, bot)
        await log_event(bot, "#error #transfer_failed", "Auto-Transfer Failed", [("📡 <b>Source:</b>", f"{src_chat}/{src_msg_id}"), ("❌ <b>Error:</b>", result.error[:300]), ("📝 <b>Note:</b>", "Manual approval request sent")])


@router.callback_query(F.data.startswith("srcappr:"))
async def approve_source(callback, bot: Bot):
    if callback.from_user.id not in OWNER_IDS:
        await callback.answer("Only owner", show_alert=True)
        return
    try:
        prefix, source_chat_id, source_message_id = callback.data.split(":")
        if prefix != "srcappr":
            raise ValueError
        source_chat_id, source_message_id = int(source_chat_id), int(source_message_id)
        if source_message_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        await callback.answer("Invalid approval request", show_alert=True)
        return
    key = _approval_key(source_chat_id, source_message_id)
    status = await db.get(key, "pending")
    if status != "pending":
        await callback.answer(f"Already {status}", show_alert=True)
        return
    try:
        source = await bot.forward_message(callback.from_user.id, source_chat_id, source_message_id, disable_notification=True)
        inserted = await index_message(source, bot=bot, storage_channel=STORAGE_CHANNEL)
        await db.set(key, "approved")
        await request_snapshot(bot)
        await callback.answer("Added to MAIN_DATA ✅")
        await callback.message.edit_reply_markup(reply_markup=None)
        result = "✅ <b>Source movie approved</b>\nMAIN_DATA me add/index ho gayi." if inserted else "ℹ️ Movie already catalog me thi; duplicate copy nahi banayi."
        await callback.message.answer(result)
    except Exception as exc:
        logging.exception("Source approval failed for %s/%s", source_chat_id, source_message_id)
        await callback.answer("Add failed", show_alert=True)
        await callback.message.answer(f"❌ MAIN_DATA me add nahi hui: <code>{str(exc)[:500]}</code>")


@router.callback_query(F.data.startswith("srcdecl:"))
async def decline_source(callback, bot: Bot):
    if callback.from_user.id not in OWNER_IDS:
        await callback.answer("Only owner", show_alert=True)
        return
    try:
        prefix, source_chat_id, source_message_id = callback.data.split(":")
        if prefix != "srcdecl":
            raise ValueError
        key = _approval_key(int(source_chat_id), int(source_message_id))
    except (TypeError, ValueError):
        await callback.answer("Invalid decline request", show_alert=True)
        return
    status = await db.get(key, "pending")
    if status != "pending":
        await callback.answer(f"Already {status}", show_alert=True)
        return
    await db.set(key, "declined")
    await callback.answer("Declined")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("❌ Source movie decline kar di gayi; MAIN_DATA me add nahi hui.")


# ---------------- historical scan ----------------
class ScanStats:
    def __init__(self) -> None:
        self.found = 0
        self.skipped = 0     # media nahi tha
        self.missing = 0     # deleted / khali id
        self.errors = 0
        self.protected = 0
        self.last_error = ""
        self.stop_reason = ""
        self.samples: list[str] = []
        self.last_scanned_id = 0

    def as_text(self) -> str:
        lines = [
            f"🎬 Indexed: <b>{self.found}</b>",
            f"⏭ Non-media skip: {self.skipped}",
            f"🕳 Deleted/empty ids: {self.missing}",
            f"⚠️ Errors: {self.errors}",
        ]
        if self.protected:
            lines.append(f"🔒 Protected-content blocks: {self.protected}")
        if self.last_error:
            lines.append(f"Last error: <code>{self.last_error}</code>")
        return "\n".join(lines)


async def _probe(bot: Bot, dump: int, src, mid: int, stats: ScanStats, dedup=None) -> None:
    """Forward a source post into MAIN_DATA and index the resulting message."""
    same_channel = str(src) == str(dump)
    # Telegram self-forwarding in the same private channel is unreliable for
    # historical reads. Probe MAIN_DATA through LOG_CHANNEL when available,
    # then delete the temporary probe immediately.
    probe_channel = LOG_CHANNEL if same_channel and LOG_CHANNEL and LOG_CHANNEL != dump else dump
    for attempt in range(5):
        try:
            fwd = await bot.forward_message(probe_channel, src, mid, disable_notification=True)
        except Exception as e:
            txt = str(e)
            kind = classify(txt)
            if kind == "flood":
                m = RETRY_RE.search(txt)
                wait = int(m.group(1)) + 1 if m else min(30, 2 ** attempt)
                await asyncio.sleep(wait)
                continue
            if kind == "missing":
                stats.missing += 1
            elif kind == "protected":
                stats.protected += 1
                stats.last_error = txt
            elif kind == "fatal":
                stats.errors += 1
                stats.last_error = txt
                stats.stop_reason = txt
            else:
                stats.errors += 1
                stats.last_error = txt
                if len(stats.samples) < 3:
                    stats.samples.append(f"msg {mid}: {txt}")
            return

        keep_copy = not same_channel
        try:
            if (fwd.caption or "").strip() == SNAPSHOT_MARKER:
                stats.skipped += 1
                return
            media = fwd.video or fwd.document or fwd.audio
            caption = fwd.caption or fwd.text or ""
            if not media:
                stats.skipped += 1
                return
            duplicate = False
            if dedup is not None:
                caption_key = " ".join(caption.split()).strip().casefold()
                async with dedup["lock"]:
                    file_key = getattr(media, "file_id", "") or ""
                    duplicate = bool(file_key and file_key in dedup["files"]) or bool(caption_key and caption_key in dedup["captions"])
                    if not duplicate:
                        if file_key:
                            dedup["files"].add(file_key)
                        if caption_key:
                            dedup["captions"].add(caption_key)
            else:
                duplicate = await db.has_duplicate_movie(getattr(media, "file_id", ""), caption)
            if duplicate:
                stats.skipped += 1
                if not same_channel:
                    try:
                        await bot.delete_message(probe_channel, fwd.message_id)
                    except Exception:
                        pass
                return
            meta = build_meta(media, caption)
            # When scanning MAIN_DATA itself, fwd.message_id is only a temporary
            # parsing copy. Store the original channel post ID so delivery uses
            # bot.copy_message(..., MAIN_DATA, original_id) directly.
            index_id = mid if same_channel else fwd.message_id
            storage_id = mid if same_channel else fwd.message_id
            inserted = await db.upsert_movie(index_id, meta["title"], meta["year"], meta["quality"],
                                             meta["language"], meta["size"], caption,
                                             media.file_id, media_kind(fwd), detect_content_type(caption),
                                             duration=meta["duration"], storage_msg_id=storage_id, commit=False)
            if inserted:
                stats.found += 1
                logging.info("Scan stored %s -> %s (%s %s)", mid, meta["title"], meta["quality"], meta["size"])
            else:
                stats.skipped += 1
            return
        finally:
            if not keep_copy:
                try:
                    await bot.delete_message(probe_channel, fwd.message_id)
                except Exception:
                    pass


async def scan_ids(bot: Bot, src, dump: int, message_ids: list[int],
                   progress=None, concurrency: int = 64) -> ScanStats:
    """Scan an explicitly enumerated set of real channel message IDs."""
    stats = ScanStats()
    ids = list(dict.fromkeys(int(mid) for mid in message_ids if int(mid) > 0))
    total = len(ids)
    sem = asyncio.Semaphore(concurrency)
    done = 0
    lock = asyncio.Lock()
    dedup_files, dedup_captions = await db.get_dedup_sets()
    dedup = {
        "files": dedup_files,
        "captions": dedup_captions,
        "lock": asyncio.Lock(),
    }

    async def worker(mid: int) -> None:
        nonlocal done
        async with sem:
            await _probe(bot, dump, src, mid, stats, dedup=dedup)
            async with lock:
                done += 1
                if progress and done % 250 == 0:
                    await progress(done, total, stats)

    for offset in range(0, total, 1000):
        batch_ids = ids[offset:offset + 1000]
        await asyncio.gather(*(worker(mid) for mid in batch_ids))
        await db.commit()
    if not stats.stop_reason:
        await db.set(f"scan_cursor:{src}", str(max(ids) if ids else 0))
        await request_snapshot(bot, delay=2)
    if progress:
        await progress(done, total, stats)
    return stats


async def scan_channel_fast(bot: Bot, src, dump: int, progress=None,
                            resume_key: str = "") -> ScanStats:
    """Fast Telethon-based scan: read messages directly (newest-first), batch-index.

    Same-channel (src == dump): pure read + index, no per-message Bot API calls.
    Different-channel: read + dedup via Telethon, copy only non-duplicates via
    Bot API with high concurrency and adaptive flood control.

    Optimizations:
    - Uses db.get_dedup_sets() instead of loading all full rows
    - Telethon batch_size=1000 for faster MTProto reads
    - Adaptive concurrency: starts high, backs off on flood errors
    - Exponential backoff retry with up to 5 attempts on flood
    - Durable resume cursor so interrupted scans continue from where they stopped
    - Periodic MongoDB saves mid-scan so progress is never lost
    - Reconnects Telethon automatically on disconnect
    """
    from mtproto_importer import _get_client
    from config import TG_API_ID, TG_API_HASH, TG_SESSION, SCAN_COMMIT_BATCH

    stats = ScanStats()
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        stats.last_error = "TG_API_ID/TG_API_HASH/TG_SESSION not configured"
        stats.stop_reason = stats.last_error
        return stats
    client = await _get_client()
    if client is None:
        stats.last_error = "TG_SESSION is not authorized"
        stats.stop_reason = stats.last_error
        return stats

    same_channel = str(src) == str(dump)
    try:
        entity = await client.get_entity(src)
    except Exception as exc:
        stats.last_error = str(exc)
        stats.stop_reason = str(exc)
        return stats

    # Load only file_id + caption for dedup — not full rows
    dedup_files, dedup_captions = await db.get_dedup_sets()

    # Resume cursor: newest→oldest walk, so max_id = last processed message.
    cursor_max = 0
    if resume_key:
        saved = await db.get(resume_key, "")
        if saved and saved.isdigit() and int(saved) > 0:
            cursor_max = int(saved)
            logging.info("Scan resuming from cursor %s (max_id=%s)", resume_key, cursor_max)

    started = time.monotonic()
    last_save = time.monotonic()
    pending: list[tuple] = []
    last_id = 0
    total = 0

    async def flush_and_save(cursor_id: int) -> None:
        """Bulk write + commit + durable cursor + periodic Mongo save."""
        nonlocal pending, last_save
        if pending:
            stats.found += await db.bulk_upsert_movies(pending)
            pending = []
        await db.commit()
        if resume_key and cursor_id:
            await db.set(resume_key, str(cursor_id))
        # Save to MongoDB every 60 seconds so progress survives crashes.
        if time.monotonic() - last_save >= 60:
            try:
                from mongo_store import save_movie_catalog
                await save_movie_catalog()
            except Exception as exc:
                logging.warning("Mid-scan Mongo save failed: %s", exc)
            last_save = time.monotonic()

    def _extract_meta(message):
        document = getattr(message, "document", None)
        if not message.media or not document:
            return None
        caption = message.message or ""
        if (caption or "").strip() == SNAPSHOT_MARKER:
            return None
        mime = getattr(document, "mime_type", "") or ""
        is_video = mime.startswith("video/")
        is_audio = mime.startswith("audio/")
        if not (is_video or is_audio or mime):
            return None
        access_hash = getattr(document, "access_hash", 0) or 0
        file_key = f"tg:{document.id}:{access_hash}"
        caption_key = " ".join(caption.split()).strip().casefold()
        attributes = getattr(document, "attributes", []) or []
        file_name = next((getattr(a, "file_name", "") for a in attributes if getattr(a, "file_name", "")), "")
        duration = next((getattr(a, "duration", 0) for a in attributes if getattr(a, "duration", 0)), 0)
        meta = parse_caption(caption or file_name)
        meta["duration"] = format_duration(duration)
        if not meta["size"]:
            size_mb = (getattr(document, "size", 0) or 0) / 1048576
            meta["size"] = f"{size_mb / 1024:.1f}GB" if size_mb > 1024 else f"{size_mb:.0f}MB"
        file_type = "video" if is_video else "audio" if is_audio else "document"
        return {
            "id": message.id, "caption": caption, "file_key": file_key,
            "caption_key": caption_key, "meta": meta, "file_type": file_type,
        }

    if same_channel:
        # batch_size controls how many messages Telethon fetches per MTProto request.
        # 1000 is optimal for large channels — fewer round-trips than the default 100.
        # Retry loop: reconnect Telethon on transient disconnects.
        for retry in range(3):
            try:
                async for message in client.iter_messages(
                    entity, limit=None, wait_time=0, batch_size=1000,
                    max_id=cursor_max or None,
                ):
                    last_id = int(getattr(message, "id", 0) or 0)
                    total += 1
                    info = _extract_meta(message)
                    if info is None:
                        stats.skipped += 1
                    elif (info["file_key"] and info["file_key"] in dedup_files) or \
                         (info["caption_key"] and info["caption_key"] in dedup_captions):
                        stats.skipped += 1
                    else:
                        m = info["meta"]
                        pending.append((
                            message.id, m["title"], m["year"], m["quality"],
                            m["language"], m["size"], m["duration"], info["caption"],
                            info["file_key"], info["file_type"], message.id,
                            detect_content_type(info["caption"]), int(time.time()),
                        ))
                        if info["file_key"]:
                            dedup_files.add(info["file_key"])
                        if info["caption_key"]:
                            dedup_captions.add(info["caption_key"])
                    if len(pending) >= SCAN_COMMIT_BATCH:
                        await flush_and_save(last_id)
                    if progress and (total % 2000 == 0 or time.monotonic() - started >= 15):
                        await progress(total, 0, stats)
                        started = time.monotonic()
                break
            except asyncio.CancelledError:
                await flush_and_save(last_id)
                raise
            except Exception as exc:
                logging.warning("Scan channel_fast same-channel error (retry %d): %s", retry + 1, exc)
                await flush_and_save(last_id)
                if retry + 1 >= 3:
                    stats.errors += 1
                    stats.last_error = str(exc)
                    break
                # Reconnect Telethon and resume from last_id
                cursor_max = last_id or cursor_max
                await asyncio.sleep(min(30, 5 * (retry + 1)))
                client = await _get_client()
                if client is None:
                    stats.last_error = "TG_SESSION lost during scan"
                    stats.stop_reason = stats.last_error
                    break
                try:
                    entity = await client.get_entity(src)
                except Exception:
                    stats.last_error = "Could not re-resolve channel entity"
                    stats.stop_reason = stats.last_error
                    break
        await flush_and_save(last_id)
    else:
        # Different-channel: stream-copy in batches instead of loading all into memory.
        # This prevents stalls on channels with 100k+ posts.
        current_concurrency = 64
        min_concurrency = 8
        copy_sem = asyncio.Semaphore(current_concurrency)
        lock = asyncio.Lock()
        flood_count = 0
        last_flood_time = 0.0

        async def copy_and_index(info: dict) -> None:
            nonlocal current_concurrency, copy_sem, flood_count, last_flood_time
            src_mid = info["id"]
            for attempt in range(5):
                async with copy_sem:
                    try:
                        fwd = await bot.copy_message(dump, src, src_mid, disable_notification=True)
                        break
                    except Exception as exc:
                        txt = str(exc)
                        kind = classify(txt)
                        if kind == "flood":
                            m = RETRY_RE.search(txt)
                            wait = int(m.group(1)) + 1 if m else min(30, 2 ** attempt)
                            async with lock:
                                flood_count += 1
                                last_flood_time = time.monotonic()
                                if flood_count % 3 == 0 and current_concurrency > min_concurrency:
                                    current_concurrency = max(min_concurrency, current_concurrency // 2)
                                    copy_sem = asyncio.Semaphore(current_concurrency)
                                    logging.warning("Scan flood control: reduced concurrency to %d", current_concurrency)
                            await asyncio.sleep(wait)
                            continue
                        elif kind == "protected":
                            async with lock:
                                stats.protected += 1
                                stats.last_error = txt
                                if stats.protected >= 5 and stats.found == 0:
                                    stats.stop_reason = "protected"
                            return
                        elif kind == "missing":
                            async with lock:
                                stats.missing += 1
                            return
                        else:
                            async with lock:
                                stats.errors += 1
                                stats.last_error = txt
                            return
            else:
                async with lock:
                    stats.errors += 1
                    stats.last_error = "max retries exceeded"
                return

            m = info["meta"]
            inserted = await db.upsert_movie(
                fwd.message_id, m["title"], m["year"], m["quality"],
                m["language"], m["size"], info["caption"],
                info["file_key"], info["file_type"], detect_content_type(info["caption"]),
                duration=m["duration"], storage_msg_id=fwd.message_id, commit=False,
            )
            async with lock:
                if inserted:
                    stats.found += 1
                else:
                    stats.skipped += 1
                    try:
                        await bot.delete_message(dump, fwd.message_id)
                    except Exception:
                        pass
                if time.monotonic() - last_flood_time > 30 and current_concurrency < 64:
                    current_concurrency = min(64, current_concurrency + 4)
                    copy_sem = asyncio.Semaphore(current_concurrency)

        # Stream batches: read 500 messages, copy them, flush, repeat.
        # This avoids loading the entire channel into memory before starting.
        batch_infos: list[dict] = []
        for retry in range(3):
            try:
                async for message in client.iter_messages(
                    entity, limit=None, wait_time=0, batch_size=1000,
                    max_id=cursor_max or None,
                ):
                    last_id = int(getattr(message, "id", 0) or 0)
                    total += 1
                    info = _extract_meta(message)
                    if info is None:
                        stats.skipped += 1
                    elif (info["file_key"] and info["file_key"] in dedup_files) or \
                         (info["caption_key"] and info["caption_key"] in dedup_captions):
                        stats.skipped += 1
                    else:
                        batch_infos.append(info)
                        if info["file_key"]:
                            dedup_files.add(info["file_key"])
                        if info["caption_key"]:
                            dedup_captions.add(info["caption_key"])

                    # Process in batches of 500 to avoid memory blow-up.
                    if len(batch_infos) >= 500:
                        if stats.stop_reason:
                            break
                        batch_slice = batch_infos[:500]
                        batch_infos = batch_infos[500:]
                        await asyncio.gather(*(copy_and_index(item) for item in batch_slice))
                        await db.commit()
                        await flush_and_save(last_id)
                    if progress and (total % 2000 == 0 or time.monotonic() - started >= 15):
                        await progress(total, 0, stats)
                        started = time.monotonic()
                break
            except asyncio.CancelledError:
                await flush_and_save(last_id)
                raise
            except Exception as exc:
                logging.warning("Scan channel_fast diff-channel error (retry %d): %s", retry + 1, exc)
                await flush_and_save(last_id)
                if retry + 1 >= 3:
                    stats.errors += 1
                    stats.last_error = str(exc)
                    break
                cursor_max = last_id or cursor_max
                await asyncio.sleep(min(30, 5 * (retry + 1)))
                client = await _get_client()
                if client is None:
                    stats.last_error = "TG_SESSION lost during scan"
                    stats.stop_reason = stats.last_error
                    break
                try:
                    entity = await client.get_entity(src)
                except Exception:
                    stats.last_error = "Could not re-resolve channel entity"
                    stats.stop_reason = stats.last_error
                    break

        # Flush remaining batch_infos
        if batch_infos and not stats.stop_reason:
            await asyncio.gather(*(copy_and_index(item) for item in batch_infos))
            await db.commit()
        await flush_and_save(last_id)

    if not stats.stop_reason:
        await db.set(f"scan_cursor:{src}", str(last_id))
        await request_snapshot(bot, delay=2)
    if progress:
        await progress(total, 0, stats)
    return stats


async def scan_range(bot: Bot, src, dump: int, start_id: int, end_id: int,
                     progress=None, concurrency: int = 32) -> ScanStats:
    """Parallel + rate-limit safe scan. Content copy nahi hota, sirf index banta hai."""
    stats = ScanStats()
    sem = asyncio.Semaphore(concurrency)
    done = 0
    step = -1 if start_id >= end_id else 1
    total = abs(end_id - start_id) + 1
    lock = asyncio.Lock()
    stop = asyncio.Event()
    dedup_files, dedup_captions = await db.get_dedup_sets()
    dedup = {
        "files": dedup_files,
        "captions": dedup_captions,
        "lock": asyncio.Lock(),
    }
    async def worker(mid: int) -> None:
        nonlocal done
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            await _probe(bot, dump, src, mid, stats, dedup=dedup)
            # Protected channel: ek hi baar detect karke ruk jao
            if stats.protected >= 5 and stats.found == 0:
                stats.stop_reason = "protected"
                stop.set()
            if stats.stop_reason and stats.stop_reason != "protected":
                stop.set()
            async with lock:
                done += 1
                if progress and done % 250 == 0:
                    await progress(done, total, stats)

    # Larger batches reduce scheduler overhead while the semaphore keeps
    # Telegram requests bounded and prevents an uncontrolled flood.
    batch = 1000
    for offset in range(0, total, batch):
        if stop.is_set():
            break
        ids = [start_id + step * i for i in range(offset, min(offset + batch, total))]
        tasks = [asyncio.create_task(worker(mid)) for mid in ids]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            stop.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await db.rollback()
            raise
        # One durable commit per batch is dramatically faster than one commit per post.
        await db.commit()
        await asyncio.sleep(0.02)   # bounded pacing; Telegram flood limits still apply
    if not stats.stop_reason:
        await db.set(f"scan_cursor:{src}", str(max(start_id, end_id)))
        await request_snapshot(bot, delay=2)
    if progress:
        await progress(done, total, stats)
    return stats
