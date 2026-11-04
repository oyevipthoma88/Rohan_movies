"""Group behaviour: strict command-only mode + hourly random "how to search" tip.

Rules in groups/supergroups:
* Bot kisi normal message ka reply NAHI karta. Sirf commands chalte hain
  (/movie Toxic, /series Mirzapur, /anime Naruto, /help, owner commands).
* Group me koi FSM "searching" state nahi bachti — pehle /movie dabane ke baad
  har normal message search ho jata tha, wo bug yahin band hota hai.
* /movie bina naam ke → bot sirf usage example deta hai.
* Har ghante (random minute pe) bot group ke kisi recently-active user ko
  tag karke batata hai kaise search kare. Purana tip delete ho jata hai.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

from db import db
from keyboards import button

logger = logging.getLogger(__name__)
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
ALLOWED_GROUP_COMMANDS = {"movie", "series", "anime", "help", "start", "request"}
ACTIVE_WINDOW = 7 * 24 * 3600   # tag only users seen in last 7 days
TIP_INTERVAL = 3600


def is_group(message: Message) -> bool:
    return message.chat is not None and message.chat.type in GROUP_TYPES


def group_command(text: str, bot_username: str) -> str | None:
    """Return command name if the text is a command meant for THIS bot."""
    if not text.startswith("/"):
        return None
    head = text.split()[0][1:]
    name, _, target = head.partition("@")
    if target and bot_username and target.lower() != bot_username.lower():
        return None  # command for another bot
    return name.lower()


class GroupGuardMiddleware(BaseMiddleware):
    """Outer message middleware: drops every non-command group message."""

    def __init__(self) -> None:
        self._username = ""

    async def __call__(self, handler: Callable[[Message, dict], Awaitable[Any]], event: Message, data: dict) -> Any:
        if not is_group(event):
            return await handler(event, data)
        bot: Bot = data["bot"]
        user = event.from_user
        if user is not None and not user.is_bot:
            try:
                await db.remember_group_member(event.chat.id, event.chat.title or "", user.id,
                                               user.full_name or "", user.username or "")
            except Exception:
                logger.exception("remember_group_member failed")
        if not self._username:
            try:
                self._username = (await bot.me()).username or ""
            except Exception:
                pass
        cmd = group_command(event.text or event.caption or "", self._username)
        state = data.get("state")
        if state is not None:
            try:
                if await state.get_state():
                    await state.clear()  # never keep search mode alive in groups
            except Exception:
                pass
        if cmd is None:
            return None  # silent: normal chat, stickers, media, forwards…
        if cmd not in ALLOWED_GROUP_COMMANDS and not (user and await _is_staff(user.id)):
            return None
        return await handler(event, data)


async def _is_staff(user_id: int) -> bool:
    from config import OWNER_IDS
    if user_id in OWNER_IDS:
        return True
    try:
        cur = await db.conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        return await cur.fetchone() is not None
    except Exception:
        return False


def group_usage_text(content_type: str = "movie") -> str:
    ex = {"movie": "Toxic", "series": "Mirzapur", "anime": "Naruto"}.get(content_type, "Toxic")
    return (f"🎬 <b>Search kaise kare?</b>\n\n"
            f"<code>/{content_type} {ex}</code>\n\n"
            "Command ke saath naam likho — spelling thodi galat ho to bhi chalega ✨")


_TIP_TEMPLATES = [
    "👋 Hey {mention}! Movie chahiye? Bas likho:\n\n<code>/movie {title}</code>",
    "🍿 {mention}, aaj kya dekh rahe ho? Try karo:\n\n<code>/movie {title}</code>",
    "🎬 {mention} pro tip: group me search ke liye\n<code>/movie naam</code> likho.\n\nExample: <code>/movie {title}</code>",
    "✨ {mention}! Web series bhi milti hai:\n<code>/series {series}</code>\n\nMovie ke liye: <code>/movie {title}</code>",
    "📢 {mention}, bot sirf command pe reply karta hai 🙂\n\n<code>/movie {title}</code>\n<code>/anime {anime}</code>",
]
_FALLBACK_TITLES = ["Toxic", "Pushpa 2", "Jawan", "Animal", "Stree 2", "Kalki 2898 AD", "Salaar"]


async def _example_title() -> str:
    try:
        rows = await db.popular_movies("movie", 10)
        titles = [r["title"] for r in rows if r["title"]]
        if titles:
            return random.choice(titles)[:40]
    except Exception:
        pass
    return random.choice(_FALLBACK_TITLES)


async def send_group_tip(bot: Bot, chat_id: int) -> bool:
    members = await db.recent_group_members(chat_id, int(time.time()) - ACTIVE_WINDOW)
    if not members:
        return False
    m = random.choice(members)
    mention = f'<a href="tg://user?id={m["user_id"]}">{escape(m["name"] or "dost")}</a>'
    text = random.choice(_TIP_TEMPLATES).format(
        mention=mention, title=escape(await _example_title()),
        series=random.choice(["Mirzapur", "Panchayat", "Farzi", "The Family Man"]),
        anime=random.choice(["Naruto", "One Piece", "Demon Slayer"]),
    )
    me = await bot.me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        button("🤖 DM me Search", url=f"https://t.me/{me.username}?start=group")]]) if me.username else None
    old = await db.get(f"group_tip_msg:{chat_id}", "")
    if old:
        try:
            await bot.delete_message(chat_id, int(old))
        except Exception:
            pass
    msg = await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
    await db.set(f"group_tip_msg:{chat_id}", str(msg.message_id))
    return True


async def group_tip_loop(bot: Bot) -> None:
    """Every hour, at a random minute, tag one random active user per group."""
    await asyncio.sleep(random.randint(60, 300))
    while True:
        if await db.get("group_tips", "1") == "1":
            for chat_id in await db.active_group_ids():
                try:
                    await send_group_tip(bot, chat_id)
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
                except TelegramForbiddenError:
                    await db.set_group_active(chat_id, False)  # bot kicked / no rights
                except Exception as exc:
                    low = str(exc).lower()
                    if "not enough rights" in low or "chat not found" in low or "kicked" in low:
                        await db.set_group_active(chat_id, False)
                    logger.warning("group tip failed for %s: %s", chat_id, exc)
                await asyncio.sleep(random.uniform(2, 6))
        # next round: 1 hour ± 10 min so it feels random
        await asyncio.sleep(TIP_INTERVAL + random.randint(-600, 600))
