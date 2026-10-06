"""Centralized deep logging to LOG_CHANNEL.

Every user/bot action sends a detailed log message to LOG_CHANNEL so the
owner has full visibility into bot activity. Each function is a no-op when
LOG_CHANNEL is not configured.
"""
import time
import logging
from html import escape
from typing import Optional

from aiogram import Bot

from config import LOG_CHANNEL

logger = logging.getLogger(__name__)

_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━"


def _ts() -> str:
    return time.strftime("%d %b %Y %H:%M:%S")


async def log_event(bot: Bot, hashtag: str, title: str, fields: list[tuple[str, str]], user=None) -> None:
    """Send a structured log message to LOG_CHANNEL.

    Args:
        bot: Bot instance
        hashtag: e.g. "#start" or "#search #delivery"
        title: bold title line
        fields: list of (label, value) pairs
        user: optional user object for tag
    """
    if not LOG_CHANNEL:
        return
    lines = [f"{hashtag}", f"<b>{title}</b>", _DIVIDER]
    if user is not None:
        uname = f"@{user.username}" if getattr(user, "username", None) else "no-username"
        lines.append(f"👤 <b>User:</b> {getattr(user, 'full_name', '') or '-'} ({uname}) <code>{user.id}</code>")
    for label, value in fields:
        safe = escape(str(value)) if value is not None else "-"
        lines.append(f"{label} <code>{safe}</code>")
    lines.append(_DIVIDER)
    lines.append(f"⏰ <b>Time:</b> {_ts()}")
    text = "\n".join(lines)
    try:
        await bot.send_message(LOG_CHANNEL, text)
    except Exception as exc:
        logger.warning("LOG_CHANNEL send failed for %s: %s", hashtag, exc)


async def log_start(bot: Bot, user, is_new: bool, ref: int, total_users: int = 0) -> None:
    fields = [
        ("🆕 <b>New:</b>", "Yes" if is_new else "No"),
        ("👥 <b>Referred by:</b>", str(ref) if ref else "-"),
    ]
    if total_users:
        fields.append(("📊 <b>Total Users:</b>", str(total_users)))
    await log_event(bot, "#start", "Bot Started", fields, user=user)


async def log_command(bot: Bot, user, command: str, args: str = "") -> None:
    fields = [
        ("📝 <b>Command:</b>", f"/{command}"),
    ]
    if args:
        fields.append(("📄 <b>Args:</b>", args))
    await log_event(bot, f"#{command}", f"Command /{command}", fields, user=user)


async def log_callback(bot: Bot, user, callback_data: str, label: str = "") -> None:
    fields = [
        ("🔘 <b>Callback:</b>", callback_data),
    ]
    if label:
        fields.append(("📝 <b>Action:</b>", label))
    await log_event(bot, "#callback", f"Button: {label or callback_data}", fields, user=user)


async def log_search(bot: Bot, user, query: str, result_count: int, content_type: str = "", top_result: str = "") -> None:
    fields = [
        ("🔍 <b>Query:</b>", query),
        ("📂 <b>Type:</b>", content_type or "movie"),
        ("📊 <b>Results:</b>", str(result_count)),
    ]
    if top_result:
        fields.append(("🏆 <b>Top:</b>", top_result))
    await log_event(bot, "#search", "Movie Search", fields, user=user)


async def log_not_found(bot: Bot, user, query: str, content_type: str = "", suggested: str = "") -> None:
    fields = [
        ("🔍 <b>Query:</b>", query),
        ("📂 <b>Type:</b>", content_type or "movie"),
    ]
    if suggested:
        fields.append(("💡 <b>Suggested:</b>", suggested))
    await log_event(bot, "#notfound #request", "Movie Not Found", fields, user=user)


async def log_delivery(bot: Bot, user, query: str, title: str, msg_id: int, mode: str, error: str = "") -> None:
    fields = [
        ("🔍 <b>Query:</b>", query or "-"),
        ("🎬 <b>Movie:</b>", title),
        ("📋 <b>Msg ID:</b>", str(msg_id)),
        ("⚙️ <b>Mode:</b>", mode),
    ]
    if error:
        fields.append(("❌ <b>Error:</b>", error[:500]))
    await log_event(bot, "#delivery", "Movie Delivered", fields, user=user)


async def log_quota_exhausted(bot: Bot, user, limit: int) -> None:
    fields = [
        ("📊 <b>Limit:</b>", str(limit)),
        ("💡 <b>Action:</b>", "Shown plans to upgrade"),
    ]
    await log_event(bot, "#quota", "Free Limit Exhausted", fields, user=user)


async def log_voice_search(bot: Bot, user, transcribed: str = "", failed: bool = False) -> None:
    fields = [
        ("🎤 <b>Transcribed:</b>", transcribed or "N/A"),
    ]
    if failed:
        fields.append(("❌ <b>Status:</b>", "Failed to process"))
    await log_event(bot, "#voice", "Voice Search", fields, user=user)


async def log_payment(bot: Bot, user, plan_label: str, pid: int) -> None:
    fields = [
        ("💳 <b>Plan:</b>", plan_label),
        ("🧾 <b>Payment #:</b>", str(pid)),
    ]
    await log_event(bot, "#payment", "Payment Screenshot Submitted", fields, user=user)


async def log_payment_approved(bot: Bot, approver_id: int, user_id: int, plan_label: str, pid: int) -> None:
    fields = [
        ("👤 <b>User ID:</b>", str(user_id)),
        ("💳 <b>Plan:</b>", plan_label),
        ("🧾 <b>Payment #:</b>", str(pid)),
        ("✅ <b>Approved by:</b>", str(approver_id)),
    ]
    await log_event(bot, "#payment #approved", "Payment Approved", fields)


async def log_payment_rejected(bot: Bot, rejector_id: int, user_id: int, pid: int) -> None:
    fields = [
        ("👤 <b>User ID:</b>", str(user_id)),
        ("🧾 <b>Payment #:</b>", str(pid)),
        ("❌ <b>Declined by:</b>", str(rejector_id)),
    ]
    await log_event(bot, "#payment #rejected", "Payment Declined", fields)


async def log_coupon_redeem(bot: Bot, user, code: str, success: bool, days: int = 0) -> None:
    fields = [
        ("🎟 <b>Code:</b>", code),
        ("✅ <b>Status:</b>", "Success" if success else "Invalid/Used"),
    ]
    if success and days:
        fields.append(("📅 <b>Days:</b>", str(days)))
    await log_event(bot, "#coupon", "Coupon Redeem Attempt", fields, user=user)


async def log_referral_view(bot: Bot, user, referral_count: int) -> None:
    fields = [
        ("👥 <b>Referrals:</b>", str(referral_count)),
    ]
    await log_event(bot, "#referral", "Referral Page Viewed", fields, user=user)


async def log_owner_action(bot: Bot, owner_id: int, action: str, details: str = "") -> None:
    fields = [
        ("👑 <b>Owner ID:</b>", str(owner_id)),
        ("📝 <b>Action:</b>", action),
    ]
    if details:
        fields.append(("📄 <b>Details:</b>", details[:500]))
    await log_event(bot, "#owner", f"Owner: {action}", fields)


async def log_broadcast(bot: Bot, owner_id: int, total: int, sent: int, failed: int) -> None:
    fields = [
        ("👑 <b>By:</b>", str(owner_id)),
        ("📊 <b>Total:</b>", str(total)),
        ("✅ <b>Sent:</b>", str(sent)),
        ("❌ <b>Failed:</b>", str(failed)),
    ]
    await log_event(bot, "#broadcast", "Broadcast Sent", fields)


async def log_join_request(bot: Bot, user, chat_id: int, chat_title: str, approved: bool) -> None:
    fields = [
        ("💬 <b>Group:</b>", chat_title),
        ("🆔 <b>Group ID:</b>", str(chat_id)),
        ("✅ <b>Approved:</b>", "Yes" if approved else "No"),
    ]
    await log_event(bot, "#joinrequest", "Join Request", fields, user=user)


async def log_auto_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    fields = [
        ("💬 <b>Chat ID:</b>", str(chat_id)),
        ("📋 <b>Message ID:</b>", str(message_id)),
    ]
    await log_event(bot, "#autodelete", "Movie Auto-Deleted", fields)


async def log_addvideo(bot: Bot, user, title: str, success: bool, error: str = "") -> None:
    fields = [
        ("🎬 <b>Title:</b>", title or "Unknown"),
        ("✅ <b>Status:</b>", "Added" if success else "Failed"),
    ]
    if error:
        fields.append(("❌ <b>Error:</b>", error[:300]))
    await log_event(bot, "#addvideo", "Admin Add Video", fields, user=user)


async def log_group_search(bot: Bot, user, chat_id: int, query: str, result_count: int) -> None:
    fields = [
        ("💬 <b>Group ID:</b>", str(chat_id)),
        ("🔍 <b>Query:</b>", query),
        ("📊 <b>Results:</b>", str(result_count)),
    ]
    await log_event(bot, "#groupsearch", "Group Movie Search", fields, user=user)


async def log_movie_request(bot: Bot, user, query: str) -> None:
    """Log when user explicitly requests a movie via /request."""
    fields = [
        ("🔍 <b>Request:</b>", query),
    ]
    await log_event(bot, "#request #movierequest", "Movie Request", fields, user=user)


async def log_new_movie_added(bot: Bot, title: str, quality: str, source_chat: int, dest_msg_id: int, method: str, total_catalog: int) -> None:
    fields = [
        ("📝 <b>Title:</b>", title),
        ("🎥 <b>Quality:</b>", quality or "Unknown"),
        ("📡 <b>Source:</b>", str(source_chat)),
        ("📦 <b>MAIN_DATA Msg:</b>", str(dest_msg_id)),
        ("🤖 <b>Method:</b>", method),
        ("📊 <b>Total Catalog:</b>", str(total_catalog)),
    ]
    await log_event(bot, "#new #movie #auto_transfer", "New Movie Added to MAIN_DATA", fields)
