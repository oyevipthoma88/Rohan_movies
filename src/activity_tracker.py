"""Global activity tracker (outer middleware on every update).

Pehle sirf search karne wale users DB/Mongo/LOG_CHANNEL me aate the.
Ab har user jo bot ko kuch bhi karta hai — /start, koi bhi button, text,
photo, voice, payment screenshot, join request, bot block/unblock — uska:

* SQLite users table me profile refresh (name, username, language, last seen)
* Mongo `users` (live profile) + `user_activity` (har event ki row)
* LOG_CHANNEL me ek detailed log card
"""
from __future__ import annotations

import logging
import time
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, InlineQuery, Message, Update

from config import LOG_CHANNEL, OWNER_IDS
from db import db
from log_helper import enqueue_log
from mongo_store import record_user_activity

logger = logging.getLogger(__name__)
_DIV = "━━━━━━━━━━━━━━━━━━━━━━━"


def _message_kind(m: Message) -> str:
    for kind in ("text", "photo", "video", "document", "audio", "voice", "video_note", "sticker",
                 "animation", "contact", "location", "poll", "dice"):
        if getattr(m, kind, None):
            return kind
    return m.content_type or "other"


def _forward_from(m: Message) -> str:
    origin = getattr(m, "forward_origin", None)
    if not origin:
        return ""
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is not None:
        return f"{chat.title or ''} ({chat.id})"
    user = getattr(origin, "sender_user", None)
    if user is not None:
        return f"{user.full_name} ({user.id})"
    return getattr(origin, "sender_user_name", "") or "hidden"


def describe(update: Update) -> tuple[Any, dict] | None:
    """Return (telegram_user, activity_dict) for an update, or None."""
    if update.message:
        m = update.message
        if m.from_user is None:
            return None
        text = m.text or m.caption or ""
        kind = _message_kind(m)
        event = "command" if text.startswith("/") else ("text" if kind == "text" else f"media_{kind}")
        act = {
            "event": event,
            "kind": kind,
            "text": text[:700],
            "command": text.split()[0][1:].split("@")[0] if text.startswith("/") else "",
            "chat_id": m.chat.id,
            "chat_type": m.chat.type,
            "chat_title": m.chat.title or "",
            "message_id": m.message_id,
            "forwarded_from": _forward_from(m),
            "media_group_id": m.media_group_id or "",
            "reply_to": m.reply_to_message.message_id if m.reply_to_message else 0,
        }
        return m.from_user, act
    if update.callback_query:
        c: CallbackQuery = update.callback_query
        label = ""
        try:
            if c.message and c.message.reply_markup:
                for row in c.message.reply_markup.inline_keyboard:
                    for b in row:
                        if b.callback_data == c.data:
                            label = b.text
        except Exception:
            pass
        return c.from_user, {
            "event": "button",
            "callback_data": c.data or "",
            "button_label": label,
            "chat_id": c.message.chat.id if c.message else 0,
            "chat_type": c.message.chat.type if c.message else "",
            "message_id": c.message.message_id if c.message else 0,
        }
    if update.my_chat_member:
        mc: ChatMemberUpdated = update.my_chat_member
        status = mc.new_chat_member.status
        if mc.chat.type == "private":
            event = "blocked_bot" if status == "kicked" else "unblocked_bot"
        else:
            event = f"bot_{status}_in_group"
        return mc.from_user, {"event": event, "chat_id": mc.chat.id, "chat_type": mc.chat.type,
                              "chat_title": mc.chat.title or "", "old_status": mc.old_chat_member.status,
                              "new_status": status}
    if update.chat_join_request:
        j: ChatJoinRequest = update.chat_join_request
        return j.from_user, {"event": "join_request", "chat_id": j.chat.id, "chat_title": j.chat.title or ""}
    if update.inline_query:
        q: InlineQuery = update.inline_query
        return q.from_user, {"event": "inline_query", "text": q.query[:300]}
    return None


def _card(user, act: dict, row, is_new: bool, ms: int, error: str) -> str:
    uname = f"@{user.username}" if user.username else "no-username"
    tags = [f"#{act['event']}", f"#u{user.id}"]
    if is_new:
        tags.append("#newuser")
    if error:
        tags.append("#error")
    e = lambda v: escape(str(v))  # noqa: E731
    lines = [" ".join(tags), f"<b>👁 User Activity: {e(act['event'])}</b>", _DIV,
             f"👤 <b>Name:</b> <a href=\"tg://user?id={user.id}\">{e(user.full_name or '-')}</a>",
             f"🔖 <b>Username:</b> {e(uname)}",
             f"🆔 <b>ID:</b> <code>{user.id}</code>",
             f"🌐 <b>Lang:</b> <code>{e(user.language_code or '-')}</code> | "
             f"⭐ <b>TG Premium:</b> {'Yes' if getattr(user, 'is_premium', False) else 'No'}"]
    if user.id in OWNER_IDS:
        lines.append("👑 <b>Role:</b> Owner")
    if row is not None:
        joined = time.strftime("%d %b %Y", time.localtime(row["joined_at"] or 0)) if row["joined_at"] else "-"
        lines.append(f"📅 <b>Joined:</b> {joined} | 🔁 <b>Actions:</b> {row['actions'] or 0}"
                     f" | 👥 <b>Refs:</b> {row['referrals'] or 0}")
        prem = int(row["premium_until"] or 0)
        if prem > time.time():
            lines.append(f"💎 <b>Bot Premium till:</b> {time.strftime('%d %b %Y %H:%M', time.localtime(prem))}")
        if row["banned"]:
            lines.append("🚫 <b>Banned:</b> Yes")
    lines.append(_DIV)
    labels = {"command": "⌨️ Command", "text": "💬 Text", "kind": "📦 Type", "callback_data": "🔘 Callback",
              "button_label": "🏷 Button", "chat_type": "💭 Chat", "chat_title": "🏠 Group",
              "chat_id": "🆔 Chat ID", "forwarded_from": "↪️ Forwarded from", "media_group_id": "🖼 Album",
              "old_status": "⬅️ Old", "new_status": "➡️ New"}
    for key, label in labels.items():
        val = act.get(key)
        if val in (None, "", 0) or (key == "kind" and val == "text"):
            continue
        if key == "text" and act.get("command"):
            label = "🧾 Full"
        lines.append(f"{label}: <code>{e(val)}</code>")
    lines.append(f"⚙️ <b>Handled in:</b> {ms} ms" + (f" | ❌ <code>{e(error[:300])}</code>" if error else " | ✅ OK"))
    lines.append(f"⏰ {time.strftime('%d %b %Y %H:%M:%S')}")
    return "\n".join(lines)


class ActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[Update, dict], Awaitable[Any]], event: Update, data: dict) -> Any:
        info = None
        try:
            info = describe(event)
        except Exception:
            logger.exception("activity describe failed")
        started = time.perf_counter()
        error = ""
        try:
            return await handler(event, data)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if info is not None:
                await self._record(data.get("bot"), info, int((time.perf_counter() - started) * 1000), error)

    async def _record(self, bot: Bot | None, info, ms: int, error: str) -> None:
        user, act = info
        if getattr(user, "is_bot", False) or (LOG_CHANNEL and act.get("chat_id") == LOG_CHANNEL):
            return
        try:
            # Runs AFTER the handler so /start referral + is_new logic stays intact.
            is_new = await db.track_user(user.id, user.username or "", user.full_name or "",
                                         user.language_code or "", bool(getattr(user, "is_premium", False)),
                                         act["event"] + (":" + (act.get("command") or act.get("callback_data") or "")))
            if act["event"] == "blocked_bot":
                await db.set_blocked(user.id, True)
            row = await db.user(user.id)
        except Exception:
            logger.exception("track_user failed")
            is_new, row = False, None
        profile = {
            "user_id": user.id,
            "name": user.full_name or "",
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "username": user.username or "",
            "language_code": user.language_code or "",
            "tg_premium": bool(getattr(user, "is_premium", False)),
            "blocked_bot": act["event"] == "blocked_bot",
        }
        if row is not None:
            profile.update({"joined_at": row["joined_at"], "premium_until": row["premium_until"],
                            "referrals": row["referrals"], "referred_by": row["referred_by"],
                            "banned": bool(row["banned"])})
        await record_user_activity(profile, {**act, "error": error, "handled_ms": ms}, is_new)
        if bot is not None:
            try:
                await enqueue_log(bot, _card(user, act, row, is_new, ms, error))
            except Exception:
                logger.exception("activity log enqueue failed")
