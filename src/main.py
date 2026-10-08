import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import indexer
import owner_handlers
import user_handlers
from config import (BOT_TOKEN, LOG_CHANNEL, TRANSFER_MODE, TRANSFER_CONCURRENCY,
                    CATALOG_MIGRATION_RETRY_DELAY, CATALOG_MIGRATION_TIMEOUT, CATALOG_SAVE_INTERVAL,
                    SCAN_COMMIT_BATCH, SHUTDOWN_SAVE_BUDGET, STATE_SAVE_INTERVAL)
from helper_pool import helper_pool
from db import db
from keyboards import results_kb
from snapshot import cancel_snapshot_task, remove_legacy_telegram_snapshots, restore_snapshot
from mongo_store import close_mongo, restore_non_movie_state, save_non_movie_state, restore_movie_catalog, save_movie_catalog
from mtproto_importer import close_mtproto
from log_helper import log_auto_delete

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main() -> None:
    async def notify(text: str) -> None:
        if not LOG_CHANNEL:
            return
        try:
            await bot.send_message(LOG_CHANNEL, text)
        except Exception as exc:
            logging.warning("LOG_CHANNEL notification failed: %s", exc)

    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing — .env file bharo (dekho .env.example)")
    await db.init()
    await db.ensure_stats()
    mongo_restored = await restore_non_movie_state()
    if mongo_restored:
        logging.info("Users/settings restored from MongoDB")
    movie_restored = await restore_movie_catalog()
    if movie_restored:
        logging.info("Movie catalog restored from MongoDB (%s movies)", await db.movie_count())
    # Restored state can carry old plan rows; realign labels/badges (and reset
    # prices only when the pricing revision changed).
    await db.sync_plan_catalog()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    legacy_migration_task = None

    async def migrate_legacy_catalog() -> None:
        # Resumable MAIN_DATA import that retries itself automatically. The
        # owner never has to send /scanmain: every timeout or network error is
        # resumed from the durable cursor stored in SQLite + MongoDB settings.
        if await db.get("catalog_migration_v1", "") == "done":
            return
        from mtproto_importer import import_main_data
        attempt = 0
        announced = False
        while True:
            attempt += 1
            try:
                if not announced:
                    await notify(
                        "🛠 <b>Catalog import background me start ho gaya</b>\n"
                        "Bot normally chalega. Import khud-b-khud resume hota rahega."
                    )
                    announced = True
                stats = await asyncio.wait_for(
                    import_main_data(resume_key="main_import_cursor", attempts=5),
                    timeout=CATALOG_MIGRATION_TIMEOUT,
                )
                await save_movie_catalog()
                if stats.last_error:
                    logging.warning("Catalog import attempt %s ended with error: %s", attempt, stats.last_error)
                    await asyncio.sleep(CATALOG_MIGRATION_RETRY_DELAY)
                    continue
                await db.set("catalog_migration_v1", "done")
                await notify(
                    f"✅ <b>Catalog import complete</b>\n"
                    f"Checked: {stats.checked} | Indexed: {stats.indexed} | Errors: {stats.errors}\n"
                    f"📚 Total catalog: <b>{await db.movie_count()}</b>"
                )
                return
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                await save_movie_catalog()
                logging.warning("Catalog import window %s timed out; auto-resuming", attempt)
                if attempt == 1 or attempt % 5 == 0:
                    await notify(
                        f"⏳ <b>Catalog import chal raha hai</b> (auto-retry #{attempt})\n"
                        f"Abhi tak indexed: <b>{await db.movie_count()}</b>\n"
                        "Koi command bhejne ki zarurat nahi — resume automatic hai."
                    )
                await asyncio.sleep(CATALOG_MIGRATION_RETRY_DELAY)
            except Exception as exc:
                logging.exception("Catalog import attempt %s failed", attempt)
                await save_movie_catalog()
                if attempt == 1 or attempt % 5 == 0:
                    await notify(
                        f"⚠️ <b>Catalog import retry ho raha hai</b> (#{attempt})\n"
                        f"<code>{str(exc)[:200]}</code>"
                    )
                await asyncio.sleep(min(300, CATALOG_MIGRATION_RETRY_DELAY * attempt))

    await notify("♻️ <b>Bot restart started</b>\nRestoring users, settings, and existing MAIN_DATA movie catalog from MongoDB.")
    removed_legacy = await remove_legacy_telegram_snapshots(bot)
    if removed_legacy:
        logging.info("Removed %s legacy Telegram state file(s)", removed_legacy)
    restored = False
    if await db.movie_count() == 0 or await db.user_count() == 0:
        logging.info("Checking snapshot for additional data...")
        restored = await restore_snapshot(bot, movies_only=bool(mongo_restored))
        if restored:
            logging.info(
                "Snapshot restored before polling: users=%s movies=%s",
                await db.user_count(), await db.movie_count(),
            )

    async def recover_incremental() -> None:
        # Restart is intentionally read-only: never launch a historical scan.
        # The catalog is populated only by explicit owner /scanall or /scan
        # commands and by new channel posts while the bot is online.
        logging.info("Automatic restart scan disabled; serving existing MAIN_DATA catalog")
        await notify(
            f"🟢 <b>Bot restarted</b>\n"
            f"No scan run kiya gaya. Existing MAIN_DATA catalog: <b>{await db.movie_count()}</b>"
        )
        # Dead code (unreachable MTProto and scan_range logic) has been removed.
    recovery_task = asyncio.create_task(recover_incremental(), name="incremental-recovery")

    async def periodic_snapshot() -> None:
        last_catalog_save = time.time()
        while True:
            await asyncio.sleep(STATE_SAVE_INTERVAL)
            try:
                expired = await db.expired_deliveries(int(time.time()))
                for row in expired:
                    try:
                        await bot.delete_message(row["chat_id"], row["message_id"])
                    except Exception as exc:
                        logging.info("Expiry delete skipped for %s/%s: %s", row["chat_id"], row["message_id"], exc)
                    finally:
                        await db.remove_expiry(row["chat_id"], row["message_id"])
                        await log_auto_delete(bot, row["chat_id"], row["message_id"])
                    try:
                        suggestions = await db.recent_movies("movie", 5, storage_only=True)
                        text = "🗑 <b>Movie auto-delete ho gayi.</b>\n\nAb tum new movie search kar sakte ho."
                        markup = None
                        if suggestions:
                            names = ", ".join(str(item["title"] or "Movie") for item in suggestions[:5])
                            text += f"\n\n✅ Abhi available suggestions:\n<b>{names}</b>\n\nNeeche se koi movie choose karo:"
                            markup = results_kb(suggestions, "suggestions")
                        await bot.send_message(row["chat_id"], text, reply_markup=markup)
                    except Exception as notify_exc:
                        logging.info("Expiry notification skipped for %s: %s", row["chat_id"], notify_exc)
                await save_non_movie_state()
                # Movie metadata is bulky; save it on a slower cadence. Only
                # metadata goes to Mongo — never any video/file content.
                if time.time() - last_catalog_save >= CATALOG_SAVE_INTERVAL:
                    await save_movie_catalog()
                    last_catalog_save = time.time()
                    logging.info("Periodic Mongo state + movie catalog saved")
                else:
                    logging.info("Periodic Mongo state saved")
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Periodic state snapshot failed")

    snapshot_task = asyncio.create_task(periodic_snapshot(), name="periodic-snapshot")
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Open main menu"),
                BotCommand(command="movie", description="Search movies"),
                BotCommand(command="series", description="Search web series"),
                BotCommand(command="anime", description="Search anime"),
                BotCommand(command="request", description="Request a movie"),
                BotCommand(command="stats", description="Your usage stats"),
                BotCommand(command="myplan", description="Check premium status"),
                BotCommand(command="help", description="Show bot commands"),
                BotCommand(command="cancel", description="Cancel active search"),
                BotCommand(command="scanall", description="Scan all real source media"),
                BotCommand(command="scanmain", description="Fast main channel scan (resumable)"),
                BotCommand(command="scanfull", description="Scan complete source history"),
            ]
        )
    except Exception as exc:
        logging.warning("Could not update Telegram command menu: %s", exc)
    # Keep polling responsive while the large MAIN_DATA rebuild runs in the
    # background. Blocking here made DM /start appear dead for many minutes.
    total_movies = await db.movie_count()
    logging.info("Bot online; no automatic recovery scan. Existing catalog=%s", total_movies)
    await notify(
        f"🟢 <b>Bot is online</b>\n"
        f"MAIN_DATA se existing catalog serve ho raha hai: <b>{total_movies}</b>\n"
        "Naya scan sirf owner ke /scanall ya custom range command se chalega."
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(indexer.router)
    dp.include_router(owner_handlers.router)
    dp.include_router(user_handlers.router)
    logging.info("Telegram bot started")
    scan_task = None
    await bot.delete_webhook(drop_pending_updates=True)
    legacy_migration_task = asyncio.create_task(migrate_legacy_catalog(), name="legacy-catalog-migration")
    try:
        # Initialize Multi-Bot Transfer System
        helper_count = await helper_pool.initialize()
        logging.info(f"Multi-Bot Transfer: {helper_count} helper bots ready")
        if helper_count > 0:
            logging.info(f"Transfer mode: {TRANSFER_MODE}, Concurrency: {TRANSFER_CONCURRENCY}")
        else:
            logging.warning("No helper bots! Set HELPER_BOT_TOKENS in .env")
        scan_task = asyncio.create_task(auto_scan_main_channel(bot))
        logging.info('Auto-scan task scheduled')
        await dp.start_polling(bot)
    finally:
        # Heroku SIGKILLs 30s after SIGTERM (Error R12). Cancel every long
        # running task first, then do one small save.
        if scan_task is not None and not scan_task.done():
            scan_task.cancel()
            try:
                await asyncio.wait_for(scan_task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        if not recovery_task.done():
            recovery_task.cancel()
        try:
            await asyncio.wait_for(recovery_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        if legacy_migration_task and not legacy_migration_task.done():
            legacy_migration_task.cancel()
        if legacy_migration_task:
            try:
                await asyncio.wait_for(legacy_migration_task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        await cancel_snapshot_task()
        if not snapshot_task.done():
            snapshot_task.cancel()
        try:
            await snapshot_task
        except asyncio.CancelledError:
            pass
        # Heroku's local filesystem is ephemeral. Persist non-movie state to
        # Mongo before shutdown; movie data is always rebuilt from MAIN_DATA.
        try:
            # Only users/settings/scan-cursor here. The movie catalog is saved
            # periodically and after every scan batch, so a big shutdown write
            # is never needed (that write is what caused Error R12).
            await asyncio.wait_for(save_non_movie_state(), timeout=SHUTDOWN_SAVE_BUDGET)
        except asyncio.TimeoutError:
            logging.warning("Final state save exceeded shutdown time budget")
        except Exception:
            logging.exception("Final state snapshot failed during shutdown")
        await bot.session.close()
        await db.close()
        await close_mongo()
        await close_mtproto()




async def auto_scan_main_channel(bot: Bot):
    """Recurring incremental Telethon indexing: scan ALL main channels in parallel.

    Runs on startup and then repeats every AUTO_SCAN_INTERVAL_SECONDS so new
    content added to source channels is automatically picked up and indexed
    into the bot's main data catalog.
    """
    from config import TG_API_ID, TG_API_HASH, TG_SESSION, MAIN_DATA, MAIN_CHANNELS
    while True:
        client = None
        try:
            await asyncio.sleep(10)
            if not TG_API_ID or not TG_API_HASH or not TG_SESSION:
                logging.warning("Auto-index: no Telethon creds")
                return
            channels = MAIN_CHANNELS if MAIN_CHANNELS else [MAIN_DATA]
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            try:
                client = TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH)
            except Exception:
                client = TelegramClient(TG_SESSION, int(TG_API_ID), TG_API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                logging.warning("Auto-index: not authorized")
                return
            tasks = [_scan_single_channel(client, ch) for ch in channels]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            total_count = sum(r for r in results if isinstance(r, int))
            await save_movie_catalog()
            if total_count > 0:
                logging.info(f"Auto-index done across {len(channels)} channels! {total_count} new movies")
            else:
                logging.info("Auto-index: no new movies found this cycle")
        except asyncio.CancelledError:
            logging.info("Auto-index cancelled (shutdown)")
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            raise
        except Exception as e:
            logging.error(f"Auto-index error: {e}")
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    logging.debug("Auto-index Telethon client cleanup failed", exc_info=True)
        try:
            from config import AUTO_SCAN_INTERVAL_SECONDS
            interval = AUTO_SCAN_INTERVAL_SECONDS
        except Exception:
            interval = 300
        await asyncio.sleep(max(60, interval))


async def _scan_single_channel(client, channel_id: int) -> int:
    """Scan a single main channel for new messages since last cursor.

    Scans newest-first so newly added movies are picked up immediately,
    not after scanning the entire channel history.
    """
    from movie_catalog import detect_content_type
    from utils import parse_caption
    BATCH = SCAN_COMMIT_BATCH
    try:
        cursor_key = f"auto_scan_cursor:{channel_id}"
        min_id = 0
        try:
            min_id = int(await db.get(cursor_key, "0") or "0")
        except ValueError:
            min_id = 0
        # Newest-first: only fetch messages newer than the last cursor.
        # This picks up newly added movies in seconds instead of scanning
        # the entire channel history every cycle.
        logging.info(f"Auto-index channel {channel_id} from min_id={min_id} (newest-first)")
        count = 0
        batch = []
        t0 = time.time()
        max_id_seen = min_id
        async for msg in client.iter_messages(channel_id, min_id=min_id, limit=None, batch_size=1000, wait_time=2):
            try:
                if not msg.media:
                    continue
                if msg.id <= min_id:
                    continue
                caption = msg.message or ""
                meta = parse_caption(caption)
                document = getattr(msg, "document", None)
                mime = getattr(document, "mime_type", "") if document else ""
                is_video = mime.startswith("video/")
                is_audio = mime.startswith("audio/")
                file_type = "video" if is_video else "audio" if is_audio else "document"
                duration = ""
                if document:
                    for attr in getattr(document, "attributes", []) or []:
                        dur = getattr(attr, "duration", 0)
                        if dur:
                            total = int(dur)
                            hours, rem = divmod(total, 3600)
                            minutes, secs = divmod(rem, 60)
                            duration = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
                            break
                    if not meta["size"]:
                        size_mb = (getattr(document, "size", 0) or 0) / 1048576
                        meta["size"] = f"{size_mb/1024:.1f}GB" if size_mb > 1024 else f"{size_mb:.0f}MB"
                access_hash = getattr(document, "access_hash", 0) if document else 0
                file_key = f"tg:{getattr(document, 'id', 0)}:{access_hash}" if document else ""
                batch.append((
                    msg.id, meta["title"], meta["year"], meta["quality"],
                    meta["language"], meta["size"], duration, caption,
                    file_key, file_type, msg.id, detect_content_type(caption), int(time.time())
                ))
                if msg.id > max_id_seen:
                    max_id_seen = msg.id
                if len(batch) >= BATCH:
                    await db.conn.executemany(
                        "INSERT OR REPLACE INTO movies (msg_id,title,year,quality,language,size,duration,caption,file_id,file_type,storage_msg_id,content_type,added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        batch
                    )
                    await db.conn.commit()
                    count += len(batch)
                    batch = []
                    elapsed = max(int(time.time()-t0), 1)
                    logging.info(f"Auto-indexed ch={channel_id} {count} ({count//elapsed}/sec)")
            except asyncio.CancelledError:
                break
            except Exception:
                continue
        if batch:
            await db.conn.executemany(
                "INSERT OR REPLACE INTO movies (msg_id,title,year,quality,language,size,duration,caption,file_id,file_type,storage_msg_id,content_type,added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch
            )
            await db.conn.commit()
            count += len(batch)
        await db.set(cursor_key, str(max_id_seen))
        return count
    except Exception as e:
        logging.error(f"Auto-index channel {channel_id} error: {e}")
        return 0

if __name__ == "__main__":
    asyncio.run(main())
