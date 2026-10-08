"""
Multi-Bot Transfer System - Smart Transfer Manager
Forward first, fallback to copy if restricted, then MTProto as last resort.
Max speed: tries all helper bots in rotation with instant failover.
"""
import asyncio
import logging
import time
from typing import Optional, Tuple, List
from dataclasses import dataclass
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from config import MAIN_DATA, MAIN_CHANNELS, TRANSFER_MODE, TRANSFER_CONCURRENCY
from helper_pool import helper_pool

logger = logging.getLogger(__name__)

@dataclass
class TransferResult:
    success: bool
    method: str = ""
    source_msg_id: int = 0
    dest_msg_id: int = 0
    error: str = ""
    bot_username: str = ""

class TransferManager:
    def __init__(self):
        self._active: int = 0
        self._done: int = 0
        self._failed: int = 0
        self._start: Optional[float] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def stats(self) -> dict:
        elapsed = time.time() - self._start if self._start else 0
        return {"active": self._active, "done": self._done, "failed": self._failed,
                "elapsed": round(elapsed, 1), "speed": round((self._done / max(elapsed, 1)) * 60, 1)}

    async def transfer_message(self, src_chat: int, msg_id: int, dest_chat: int = None, mode: str = None) -> TransferResult:
        dest = dest_chat or MAIN_DATA
        transfer_mode = mode or TRANSFER_MODE
        result = TransferResult(source_msg_id=msg_id)
        # Pick least-loaded main channel for destination distribution
        if dest_chat is None and len(MAIN_CHANNELS) > 1:
            dest = MAIN_CHANNELS[result.source_msg_id % len(MAIN_CHANNELS)]

        # Try each active helper bot in rotation until one succeeds
        active = helper_pool.active_bots
        for _ in range(len(active)):
            bot = await helper_pool.get_next_bot()
            if not bot:
                break
            result.bot_username = bot.username
            try:
                # Step 1: Try forward (fastest)
                if transfer_mode in ("auto", "forward"):
                    try:
                        fwd = await asyncio.wait_for(
                            bot.bot.forward_message(chat_id=dest, from_chat_id=src_chat, message_id=msg_id),
                            timeout=10
                        )
                        result.success, result.method, result.dest_msg_id = True, "forward", fwd.message_id
                        await helper_pool.mark_success(bot)
                        async with self._lock:
                            self._done += 1
                        return result
                    except (TelegramBadRequest, TelegramForbiddenError) as e:
                        err = str(e).lower()
                        if any(x in err for x in ["restrict", "forbidden", "not allowed", "copy forbidden"]):
                            if transfer_mode == "forward":
                                continue  # try next bot
                        else:
                            raise
                    except asyncio.TimeoutError:
                        logger.warning("Forward timeout with @%s, trying next bot", bot.username)
                        continue

                # Step 2: Try copy (works when forward is restricted)
                if transfer_mode in ("auto", "copy"):
                    try:
                        cpy = await asyncio.wait_for(
                            bot.bot.copy_message(chat_id=dest, from_chat_id=src_chat, message_id=msg_id),
                            timeout=10
                        )
                        result.success, result.method, result.dest_msg_id = True, "copy", cpy.message_id
                        await helper_pool.mark_success(bot)
                        async with self._lock:
                            self._done += 1
                        return result
                    except (TelegramBadRequest, TelegramForbiddenError) as e:
                        err = str(e).lower()
                        if any(x in err for x in ["restrict", "forbidden", "not allowed"]):
                            continue  # try next bot
                        raise
                    except asyncio.TimeoutError:
                        logger.warning("Copy timeout with @%s, trying next bot", bot.username)
                        continue

            except Exception as e:
                result.error = f"Error: {str(e)[:100]}"
                await helper_pool.mark_failure(bot, result.error)
                continue  # try next bot

        # Step 3: Last resort - MTProto via Telethon session
        if not result.success:
            try:
                mtproto_result = await asyncio.wait_for(
                    _mtproto_transfer(src_chat, msg_id, dest),
                    timeout=30
                )
                if mtproto_result:
                    result.success = True
                    result.method = "mtproto"
                    result.dest_msg_id = mtproto_result
                    async with self._lock:
                        self._done += 1
                    return result
            except Exception as e:
                result.error = f"MTProto fallback failed: {str(e)[:100]}"
                logger.warning("MTProto transfer fallback failed: %s", e)

        if not result.success:
            async with self._lock:
                self._failed += 1
        return result

    async def transfer_batch(self, src_chat: int, msg_ids: List[int], dest_chat: int = None, progress=None, concurrency: int = None) -> Tuple[int, int, int]:
        dest = dest_chat or MAIN_DATA
        requested_concurrency = concurrency if concurrency is not None else TRANSFER_CONCURRENCY
        sem = asyncio.Semaphore(max(1, int(requested_concurrency)))
        success, failed, skipped, done = 0, 0, 0, 0
        total = len(msg_ids)
        self._start = time.time()

        async def _do(mid):
            nonlocal success, failed, skipped, done
            async with sem:
                async with self._lock:
                    self._active += 1
                try:
                    r = await self.transfer_message(src_chat, mid, dest)
                    if r.success:
                        success += 1
                    elif "already" in r.error.lower():
                        skipped += 1
                    else:
                        failed += 1
                finally:
                    async with self._lock:
                        self._active -= 1
                        done += 1
                    if progress and done % 10 == 0:
                        await progress(done, total, self.stats)

        await asyncio.gather(*[asyncio.create_task(_do(m)) for m in msg_ids], return_exceptions=True)
        return success, failed, skipped

    def reset(self):
        self._done = self._failed = 0
        self._start = None


async def _mtproto_transfer(src_chat: int, msg_id: int, dest_chat: int) -> int:
    """Use Telethon user session to forward a message when all helper bots fail."""
    from config import TG_API_ID, TG_API_HASH, TG_SESSION
    if not TG_API_ID or not TG_API_HASH or not TG_SESSION:
        return 0
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return 0
        msg = await client.get_messages(src_chat, ids=msg_id)
        if not msg:
            return 0
        forwarded = await msg.forward_to(dest_chat)
        return forwarded.id if forwarded else 0
    finally:
        await client.disconnect()


transfer_manager = TransferManager()
