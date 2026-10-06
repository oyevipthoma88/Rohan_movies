"""Direct Channel Search - Instant movie search across multiple main channels."""
import asyncio
import logging
from typing import List

from config import TG_API_ID, TG_API_HASH, TG_SESSION, MAIN_DATA, MAIN_CHANNELS

logger = logging.getLogger(__name__)
_client = None
_lock = asyncio.Lock()


async def get_client():
    global _client
    async with _lock:
        if _client is not None:
            try:
                if _client.is_connected():
                    return _client
            except Exception:
                pass
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            try:
                _client = TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH)
            except Exception:
                _client = TelegramClient(TG_SESSION, int(TG_API_ID), TG_API_HASH)
            await _client.connect()
            if not await _client.is_user_authorized():
                _client = None
                return None
            return _client
        except Exception as e:
            logger.error(f"Search client error: {e}")
            _client = None
            return None


async def search_channel(query: str, limit: int = 10) -> List[dict]:
    """Search movies DIRECTLY across ALL configured main channels.
    Multiple channels = parallel search = much faster for 100k+ movies."""
    if not query or not query.strip():
        return []
    client = await get_client()
    if not client:
        return []
    channels = MAIN_CHANNELS if MAIN_CHANNELS else [MAIN_DATA]
    results = []
    tasks = [_search_single_channel(client, ch, query, limit) for ch in channels]
    channel_results = await asyncio.gather(*tasks, return_exceptions=True)
    for ch_results in channel_results:
        if isinstance(ch_results, list):
            results.extend(ch_results)
    logger.info("Direct search '%s' across %d channels: %d found", query, len(channels), len(results))
    return results[:limit * 2]


async def _search_single_channel(client, channel_id: int, query: str, limit: int) -> List[dict]:
    """Search a single channel for movies."""
    results = []
    try:
        async for msg in client.iter_messages(channel_id, search=query.strip()[:100], limit=limit):
            if msg.media:
                caption = msg.message or ""
                title = caption.split(chr(10))[0][:200].strip() if caption else f"Movie {msg.id}"
                results.append({"msg_id": msg.id, "channel_id": channel_id, "title": title, "caption": caption})
    except Exception as e:
        logger.error(f"Search error on channel {channel_id}: {e}")
    return results


async def deliver_movie(bot, user_id: int, channel_id: int, msg_id: int, caption: str = None) -> bool:
    """Deliver movie directly from the specified channel to user."""
    try:
        if caption:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=channel_id,
                message_id=msg_id,
                caption=caption,
                parse_mode="HTML"
            )
        else:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=channel_id,
                message_id=msg_id
            )
        return True
    except Exception:
        try:
            await bot.forward_message(chat_id=user_id, from_chat_id=channel_id, message_id=msg_id)
            return True
        except Exception as e:
            logger.error(f"Delivery failed: {e}")
            return False
