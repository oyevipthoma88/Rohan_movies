"""Compatibility wrapper for MongoDB-only bot-state persistence.

Movie data remains in MAIN_DATA and is rebuilt from the channel on restart.
No state document is sent, copied, or pinned in Telegram anymore.
"""
from __future__ import annotations

import asyncio
import logging

from db import db
from mongo_store import save_non_movie_state, restore_non_movie_state

SNAPSHOT_MARKER = "ROHAN_MOVIES_STATE_V1"  # legacy marker; no new Telegram files use it
_snapshot_task: asyncio.Task | None = None


async def create_snapshot(bot=None) -> int | None:
    """Persist users and bot state to MongoDB; never send a Telegram file."""

    # SAFETY: Don't create snapshot if we have fewer users than before
    try:
        current_users = await db.user_count()
        if current_users == 0:
            logging.warning("Snapshot skipped: 0 users (preventing data loss)")
            return None
    except Exception:
        pass
    return 1 if await save_non_movie_state() else None


async def restore_snapshot(bot=None, movies_only: bool = False) -> bool:
    """Restore non-movie state from MongoDB; movie rows are channel-derived.

    When movies_only is True the caller has already restored non-movie state,
    so we skip the redundant restore to avoid re-importing the same data."""
    if movies_only:
        return True
    return await restore_non_movie_state()


async def remove_legacy_telegram_snapshots(bot) -> int:
    """Delete old state files previously pinned in Telegram, never movie posts."""
    from config import LOG_CHANNEL, MAIN_DATA
    removed = 0
    chats = [MAIN_DATA] + ([LOG_CHANNEL] if LOG_CHANNEL and LOG_CHANNEL != MAIN_DATA else [])
    for chat_id in chats:
        try:
            chat = await bot.get_chat(chat_id)
            pinned = chat.pinned_message
            if pinned and pinned.document and pinned.caption == SNAPSHOT_MARKER:
                await bot.delete_message(chat_id, pinned.message_id)
                removed += 1
        except Exception:
            pass
    return removed


async def request_snapshot(bot=None, delay: float = 15.0) -> None:
    """Debounce a MongoDB state save without creating Telegram messages."""
    global _snapshot_task
    if _snapshot_task and not _snapshot_task.done():
        return

    async def worker() -> None:
        await asyncio.sleep(delay)
        try:
            await save_non_movie_state()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Persistence helpers log their own failures.
            pass

    _snapshot_task = asyncio.create_task(worker(), name="mongo-state-save")


async def cancel_snapshot_task() -> None:
    global _snapshot_task
    if _snapshot_task and not _snapshot_task.done():
        _snapshot_task.cancel()
        try:
            await _snapshot_task
        except asyncio.CancelledError:
            pass
    _snapshot_task = None


__all__ = ["create_snapshot", "restore_snapshot", "remove_legacy_telegram_snapshots", "request_snapshot", "cancel_snapshot_task"]
