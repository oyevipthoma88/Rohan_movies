from __future__ import annotations

from typing import Optional
import logging
import asyncio
import time
import os
import tempfile
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import (
    DEFAULT_CAPTION_JOIN, DEFAULT_CAPTION_VISIT, LOG_CHANNEL,
    ONLINE_PLAYER_BOT, OWNER_IDS, REFERRAL_REWARDS, STORAGE_CHANNEL, SUPPORT_URL,
)
from log_helper import (
    log_start, log_command, log_callback, log_search, log_not_found,
    log_delivery, log_quota_exhausted, log_voice_search, log_payment,
    log_payment_approved, log_payment_rejected, log_coupon_redeem,
    log_referral_view, log_join_request, log_auto_delete, log_addvideo,
    log_group_search, log_movie_request,
)
from db import db
from keyboards import (admin_panel_kb, approve_kb, browse_kb, button, force_join_kb, main_menu, movie_link_kb,
                       paid_kb, plans_kb, plans_text, plan_label, results_kb)
from indexer import classify, index_message
from snapshot import request_snapshot
from movie_catalog import catalog_label
from channel_search import search_channel
from spelling import correct_spelling
from utils import (get_movie_channel, apply_referral_rewards, check_quota,
                   human_left, is_first_calendar_day, is_premium, post_link, search_movies,
                   transcribe_voice, user_tag, reserve_quota, refund_quota)

router = Router()

DEFAULT_CAPTION_TEMPLATE = (
    "🎬 <b>𝐌𝐨𝐯𝐢𝐞 :</b> {movie_name}\n\n"
    "🎥 <b>𝐐𝐮𝐚𝐥𝐢𝘁𝘆 :</b> {quality}\n"
    "⏱️ <b>𝐃𝐮𝐫𝐚𝐭𝐢𝐨𝐧 :</b> {duration}\n\n"
    "📢 <b>𝐉𝐨𝐢𝐧 :</b> {join}\n"
    "🌐 <b>𝐕𝐢𝐬𝐢𝐭 :</b> {username}\n\n"
    "⚡ <b>𝐄𝐧𝐣𝐨𝐲 𝐘𝐨𝐮𝐫 𝐌𝐨𝐯𝐢𝐞!</b> 🍿"
)


class Flow(StatesGroup):
    searching = State()
    coupon = State()
    screenshot = State()
    addvideo = State()


async def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


async def force_channels() -> list[str]:
    raw = await db.get("force_channels", "")
    # Keep the runtime invariant even if an older database contains more entries.
    return [c.strip() for c in raw.split(",") if c.strip()][:5]


async def not_joined(bot: Bot, user_id: int) -> list[tuple[str, str]]:
    pending = []
    for ch in await force_channels():
        try:
            member = await bot.get_chat_member(ch, user_id)
            status = getattr(member.status, "value", member.status)
            if status in {ChatMemberStatus.LEFT.value, ChatMemberStatus.KICKED.value}:
                raise ValueError("not joined")
        except Exception as exc:
            try:
                chat = await bot.get_chat(ch)
                title = chat.title or ch
                link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else ch)
            except Exception:
                if str(ch).startswith("https://t.me/+") or str(ch).startswith("https://t.me/joinchat/"):
                    title, link = "Private Channel", str(ch)
                else:
                    title, link = ch, f"https://t.me/{ch.lstrip('@')}"
            pending.append((title, link))
            logging.warning("Force-join check pending for %s/%s: %s", user_id, ch, exc)
    return pending


@router.chat_join_request()
async def handle_join_request(join_request: ChatJoinRequest, bot: Bot):
    """Auto-approve join requests for private GROUPS only (not channels).

    Private groups set as must-join (force_channels) me join request aate hi
    auto-approve. Private channels ke join requests skip hote hain.
    """
    chat = join_request.chat
    user = join_request.from_user

    must_join = await force_channels()
    chat_id_str = str(chat.id)
    chat_match = False
    for entry in must_join:
        entry = entry.strip()
        if not entry:
            continue
        if entry.lstrip("-").isdigit() and int(entry) == chat.id:
            chat_match = True
            break
        if chat.username and entry.lstrip("@").lower() == chat.username.lower():
            chat_match = True
            break

    if not chat_match:
        return

    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        logging.info("Join request for %s (type=%s) skipped — not a group", chat.id, chat.type)
        return

    try:
        await bot.approve_chat_join_request(chat.id, user.id)
        logging.info("Auto-approved join request: user=%s -> group=%s (%s)", user.id, chat.id, chat.title)
        await log_join_request(bot, user, chat.id, chat.title or str(chat.id), approved=True)
    except Exception as exc:
        logging.warning("Auto-approve failed for user=%s group=%s: %s", user.id, chat.id, exc)
        await log_join_request(bot, user, chat.id, chat.title or str(chat.id), approved=False)


def _expiry_label(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    hours, remainder = divmod(minutes, 60)
    if remainder:
        return f"{hours}h {remainder}m"
    return f"{hours} hour" + ("" if hours == 1 else "s")


async def format_movie_caption(bot: Bot, movie) -> str:
    title = escape(str(movie["title"] or "Movie"))
    quality = escape(str(movie["quality"] or "Unknown"))
    duration = escape(str(movie["duration"] or "Unknown"))
    try:
        storage = await bot.get_chat(STORAGE_CHANNEL)
        join = await db.get("caption_join", "") or DEFAULT_CAPTION_JOIN or (f"@{storage.username}" if storage.username else str(STORAGE_CHANNEL))
    except Exception:
        join = await db.get("caption_join", "") or DEFAULT_CAPTION_JOIN or str(STORAGE_CHANNEL)
    try:
        me = await bot.me()
        username = await db.get("caption_visit", "") or DEFAULT_CAPTION_VISIT or (f"@{me.username}" if me.username else "")
    except Exception:
        username = await db.get("caption_visit", "") or DEFAULT_CAPTION_VISIT
    # Owner-set custom template takes priority over the hardcoded default.
    template = await db.get("caption_template", "") or DEFAULT_CAPTION_TEMPLATE
    extra = ""
    values = {
        "{movie_name}": title,
        "{quality}": quality,
        "{duration}": duration,
        "{join}": escape(join),
        "{username}": escape(username or "-"),
        "{extra}": escape(extra),
    }
    # Literal token replacement is safer than str.format: unknown braces in a
    # custom caption never crash delivery and supported tokens are deterministic.
    caption = template
    for token, replacement in values.items():
        caption = caption.replace(token, replacement)
    # Delivery captions are intentionally strict: never append the source
    # channel's original caption or extra metadata outside the approved format.
    try:
        auto_delete_minutes = int(await db.get("auto_delete_minutes", "0") or 0)
    except (TypeError, ValueError):
        auto_delete_minutes = 0
    if 1 <= auto_delete_minutes <= 1440:
        caption += (
            f"\n\n⚠️ <b>Security:</b> Ye content {_expiry_label(auto_delete_minutes)} me auto-delete hoga. "
            "Movie milte hi save/forward kar lo."
        )
    return caption[:1024]


async def send_home(target: Optional[Message], bot: Bot, user_id: int) -> None:
    user = await db.user(user_id)
    if not user:
        return
    if is_premium(user):
        status = f"💎 Premium ({human_left(user['premium_until'])})"
    else:
        first_day = not bool(user["first_bonus_used"])
        status = "🆓 Free plan • Day 1: up to 3 movies" if first_day else "🆓 Free plan • 1 movie per day"
    try:
        bot_profile = await bot.me()
        bot_name = escape(bot_profile.first_name or bot_profile.username or "this bot")
    except Exception:
        bot_name = "this bot"
    suggestions = await db.popular_movies("movie", 3)
    if len(suggestions) < 3:
        existing = {str(row["msg_id"]) for row in suggestions}
        for row in await db.recent_movies("movie", 3, storage_only=True):
            if str(row["msg_id"]) not in existing:
                suggestions.append(row)
    suggestion_text = ", ".join(str(row["title"] or "Movie") for row in suggestions[:3])
    suggestion_line = f"🔥 Try these: <b>{escape(suggestion_text)}</b>\n\n" if suggestion_text else ""
    text = (
        f"👋 <b>Welcome to {bot_name}</b>\n\n"
        f"<b>{status}</b>\n\n"
        "🎬 <b>Search any movie by name</b> — spelling mistakes welcome!\n"
        "⚡ Instant delivery • 🎥 Watch Online • 🔥 Trending picks\n\n"
        f"{suggestion_line}"
        "🎁 <b>Free plan:</b> Day 1 = 3 movies, then 1 daily.\n"
        "💎 <b>Premium:</b> Unlimited movies, no daily limit.\n\n"
        "👇 Movie ka naam seedha bhejo ya button choose karo."
    )
    pic = await db.get("start_pic", "")
    kb = main_menu(user_id in OWNER_IDS, user_id in await db.admins())
    
    if target is not None:
        if pic:
            try:
                await target.answer_photo(pic, caption=text, reply_markup=kb)
                return
            except Exception:
                pass
        try:
            await target.answer(text, reply_markup=kb)
            return
        except Exception:
            pass
            
    # Fallback to sending a new message if target is None or editing failed (e.g., message deleted)
    if pic:
        try:
            await bot.send_photo(user_id, photo=pic, caption=text, reply_markup=kb)
            return
        except Exception:
            pass
    await bot.send_message(user_id, text, reply_markup=kb)


@router.message(CommandStart())
async def start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    u = message.from_user
    user = await db.user(u.id)
    if user and user["banned"]:
        return
    ref = 0
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            ref = int(parts[1][4:])
        except ValueError:
            ref = 0
    if ref == u.id:
        ref = 0
    is_new = await db.add_user(u.id, u.username or "", u.full_name, ref)
    await db.add_audit(u.id, "start", f"new={is_new} ref={ref or '-'}")
    total_users = await db.user_count(include_banned=True) if is_new else 0
    await log_start(bot, u, is_new, ref, total_users)
    if is_new:
        if ref:
            msg = await apply_referral_rewards(ref)
            try:
                await bot.send_message(ref, f"👥 Naya referral join hua!\n{msg or ''}")
            except Exception:
                pass

    pending = await not_joined(bot, u.id)
    if pending:
        await message.answer(
            "🔒 <b>Bot use karne se pehle ye channels join karo</b>\n\n"
            "Join karne ke baad <b>Verify</b> dabao.",
            reply_markup=force_join_kb(pending),
        )
        return
    await send_home(message, bot, u.id)


@router.callback_query(F.data == "verify")
async def verify(cb: CallbackQuery, bot: Bot):
    pending = await not_joined(bot, cb.from_user.id)
    if pending:
        await cb.answer("Abhi bhi join nahi kiya ❌", show_alert=True)
        return
    await cb.answer("Verified ✅")
    await log_callback(bot, cb.from_user, "verify", "Verified channel membership")
    try:
        await cb.message.delete()
    except Exception:
        pass
    await send_home(None, bot, cb.from_user.id)


@router.callback_query(F.data == "home")
async def home(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    await cb.answer()
    await log_callback(bot, cb.from_user, "home", "Home menu")
    await send_home(cb.message, bot, cb.from_user.id)


@router.callback_query(F.data == "get_movie")
async def get_movie(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await log_callback(bot, cb.from_user, "get_movie", "Opened movie search")
    await begin_catalog_search(cb.message, bot, state, cb.from_user.id, "movie")


@router.callback_query(F.data == "get_series")
async def get_series(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await log_callback(bot, cb.from_user, "get_series", "Opened web series search")
    await begin_catalog_search(cb.message, bot, state, cb.from_user.id, "series")


@router.callback_query(F.data == "get_anime")
async def get_anime(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await log_callback(bot, cb.from_user, "get_anime", "Opened anime search")
    await begin_catalog_search(cb.message, bot, state, cb.from_user.id, "anime")


async def begin_catalog_search(target: Message, bot: Bot, state: FSMContext, user_id: int, content_type: str) -> None:
    if await not_joined(bot, user_id):
        await target.answer("🔒 Pehle channels join karke /start par Verify dabao.")
        return
    ok, why = await check_quota(user_id)
    if not ok:
        await target.answer(why, reply_markup=plans_kb(await db.plans()))
        return
    await state.set_state(Flow.searching)
    await state.update_data(content_type=content_type)
    await target.answer(
        f"{catalog_label(content_type)} ka naam bhejo (spelling galat ho to bhi chalega).\n"
        "🎤 Voice note bhi bhej sakte ho."
    )


async def catalog_command(message: Message, bot: Bot, state: FSMContext, content_type: str) -> None:
    if await not_joined(bot, message.from_user.id):
        await message.answer("🔒 Pehle channels join karke /start par Verify dabao.")
        return
    query = (message.text or "").partition(" ")[2].strip()
    await state.update_data(content_type=content_type)
    if query:
        await do_search(message, query, state)
        return
    await begin_catalog_search(message, bot, state, message.from_user.id, content_type)


@router.message(Command("movie"))
async def movie_command(message: Message, bot: Bot, state: FSMContext):
    await log_command(bot, message.from_user, "movie", (message.text or "").partition(" ")[2])
    await catalog_command(message, bot, state, "movie")


@router.message(Command("series"))
async def series_command(message: Message, bot: Bot, state: FSMContext):
    await log_command(bot, message.from_user, "series", (message.text or "").partition(" ")[2])
    await catalog_command(message, bot, state, "series")


@router.message(Command("anime"))
async def anime_command(message: Message, bot: Bot, state: FSMContext):
    await log_command(bot, message.from_user, "anime", (message.text or "").partition(" ")[2])
    await catalog_command(message, bot, state, "anime")


@router.callback_query(F.data == "admin:addvideo")
async def admin_addvideo_button(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in await db.admins():
        await cb.answer("Sirf admin ke liye", show_alert=True)
        return
    await cb.answer()
    await state.set_state(Flow.addvideo)
    await cb.message.answer("➕ Configured movie channel ka video yahan forward karo.\n/cancel se cancel kar sakte ho.", reply_markup=admin_panel_kb())


@router.message(Command("addvideo"))
async def admin_addvideo_command(message: Message, bot: Bot, state: FSMContext):
    if message.from_user.id not in await db.admins():
        return
    await log_command(bot, message.from_user, "addvideo")
    await state.set_state(Flow.addvideo)
    await message.answer("➕ Configured movie channel ka video yahan forward karo.\n/cancel se cancel kar sakte ho.", reply_markup=admin_panel_kb())


@router.message(Flow.addvideo)
async def admin_addvideo(message: Message, bot: Bot, state: FSMContext):
    if message.from_user.id not in await db.admins():
        await state.clear()
        return
    if not (message.video or message.document or message.audio):
        await message.answer("Video/document/audio forward karo, ya /cancel bhejo.")
        return
    try:
        indexed = await index_message(message, bot=bot, storage_channel=STORAGE_CHANNEL)
        if indexed:
            await request_snapshot(bot)
        await log_addvideo(bot, message.from_user, str(getattr(message.video or message.document or message.audio, "file_name", "")), bool(indexed))
    except Exception as exc:
        await log_addvideo(bot, message.from_user, "", False, str(exc))
        await message.answer(f"❌ Video add nahi hui: <code>{str(exc)[:180]}</code>")
        return
    await state.clear()
    await message.answer("✅ Video catalog me add ho gayi. Admin sirf video add kar sakta hai.", reply_markup=admin_panel_kb()) if indexed else await message.answer("❌ Media detect nahi hua.")


@router.message(Command("help"))
async def help_command(message: Message, bot: Bot):
    await log_command(bot, message.from_user, "help")
    await message.answer(
        "📘 <b>Commands</b>\n\n"
        "/movie [name] — movie search\n"
        "/series [name] — web series search\n"
        "/anime [name] — anime search\n"
        "/myplan — premium status\n"
        "/cancel — active search cancel\n"
        "/request [name] — movie request\n"
        "/stats — your usage stats\n\n"
        "🎁 Free users: first day 3 movies, then 1 movie per day."
    )


@router.message(Command("cancel"))
async def cancel_command(message: Message, bot: Bot, state: FSMContext):
    await log_command(bot, message.from_user, "cancel")
    await state.clear()
    await message.answer("Active search cancel ho gaya. Naya search /movie se start karo.")


@router.message(Flow.searching, F.voice | F.audio)
async def voice_search(message: Message, bot: Bot, state: FSMContext):
    note = await message.answer("🎤 Voice sun raha hoon...")
    fd, path = tempfile.mkstemp(prefix="rohan_voice_", suffix=".ogg")
    os.close(fd)
    try:
        file = await bot.get_file((message.voice or message.audio).file_id)
        await bot.download_file(file.file_path, path)
        text = await transcribe_voice(path)
        if not text:
            await log_voice_search(bot, message.from_user, failed=True)
            await note.edit_text("Voice samajh nahi aayi 😕 Movie ka naam type karke bhejo.")
            return
        await log_voice_search(bot, message.from_user, text)
        await note.edit_text(f"🔎 Suna: <b>{escape(text)}</b>")
        await do_search(message, text, state)
    except Exception:
        logging.exception("Voice search failed for %s", message.from_user.id)
        await log_voice_search(bot, message.from_user, failed=True)
        await note.edit_text("Voice process nahi ho payi 😕 Movie ka naam type karke bhejo.")
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@router.message(Flow.searching, F.text)
async def text_search(message: Message, state: FSMContext):
    await do_search(message, message.text.strip(), state)


@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def direct_dm_search(message: Message, state: FSMContext):
    """Search any plain DM text without requiring the Movies button first."""
    await state.update_data(content_type="movie")
    await do_search(message, message.text.strip(), state)


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text, ~F.text.startswith("/"))
async def direct_group_search(message: Message, bot: Bot, state: FSMContext):
    """In groups, only respond when the bot is @mentioned or replied to.

    Owner can disable group search entirely via the Group Mode toggle in the
    owner panel (db key: group_mode, default '1')."""
    group_mode = await db.get("group_mode", "1")
    if group_mode != "1":
        return
    me = await bot.me()
    username = f"@{me.username.lower()}" if me.username else ""
    is_mentioned = username and username in (message.text or "").lower()
    is_reply_to_bot = (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == me.id
    )
    if not is_mentioned and not is_reply_to_bot:
        return
    query = (message.text or "").replace(username, "").strip() if is_mentioned else (message.text or "").strip()
    if not query:
        return
    await state.update_data(content_type="movie")
    await log_group_search(bot, message.from_user, message.chat.id, query, 0)
    await do_search(message, query, state)




async def do_search(message: Message, query: str, state: FSMContext):
    pending = await not_joined(message.bot, message.from_user.id)
    if pending:
        await message.answer(
            "🔒 Required channel leave ho gaya hai. Movie search/delivery ke liye pehle join karo aur Verify dabao.",
            reply_markup=force_join_kb(pending),
        )
        return
    user = await db.user(message.from_user.id)
    if user and user["banned"]:
        return
    if not user:
        await db.add_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name)
        await db.add_audit(message.from_user.id, "auto_register", "registered from search")
    await db.add_audit(message.from_user.id, "search", query)
    ok, why = await check_quota(message.from_user.id)
    if not ok:
        await state.clear()
        await log_quota_exhausted(bot=message.bot, user=message.from_user, limit=1)
        await message.answer(why, reply_markup=plans_kb(await db.plans()))
        return
    data = await state.get_data()
    content_type = data.get("content_type", "")
    results = await search_movies(query, content_type=content_type)

    # ── DIRECT CHANNEL FALLBACK: channel se instant results ──
    if not results:
        try:
            direct_results = await search_channel(query, limit=50)
            logging.info("Direct search '%s': %s found", query, len(direct_results or []))
        except Exception as _e:
            logging.warning("Direct search error: %s", _e)
            direct_results = []
        if direct_results:
            _seen = set()
            _rows = []
            for _d in direct_results:
                _k = (_d.get('title') or '').strip()[:60].lower()
                if _k and _k not in _seen:
                    _seen.add(_k)
                    _rows.append(_d)
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            _btns = []
            for _r in _rows[:10]:
                title = (_r.get('title') or 'Movie')[:60]
                _ch_id = _r.get('channel_id', STORAGE_CHANNEL)
                _btns.append([InlineKeyboardButton(
                    text=f"🎬 {title}",
                    callback_data=f"directdl:{_ch_id}:{_r.get('msg_id')}"
                )])
            # Support button when available
            _support_url = await db.get("support_url", "") or SUPPORT_URL
            if _support_url:
                _btns.append([InlineKeyboardButton(text="📞 Support / Request Movie", url=_support_url)])
            _btns.append([InlineKeyboardButton(text="🏠 Home", callback_data="home")])
            await message.answer(
                f"✅ <b>{escape(query)}</b> ke liye {len(_rows)} results mile!\nMovie select karo 👇",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=_btns)
            )
            return

        if not results:
            pass  # Show suggestions when no results
        suggestions = await db.recent_movies(content_type=content_type, limit=5, storage_only=True)
        suggestion_text = ""
        suggestion_markup = None
        if suggestions:
            names = ", ".join(str(row["title"] or "Movie") for row in suggestions[:5])
            suggestion_text = f"\n\n✅ Ye movies abhi available hain: <b>{escape(names)}</b>\nInme se kisi button ko tap karke movie le sakte ho:"
            suggestion_markup = results_kb(suggestions, query)
        # Get support URL from settings or env
        support_url = await db.get("support_url", "") or SUPPORT_URL
        
        # Try spelling correction using SQL-filtered candidates (not all titles)
        candidate_titles = await db.search_titles_for_spelling(query)
        corrected = correct_spelling(query, candidate_titles)
        spelling_msg = ""
        if corrected and corrected.lower() != query.lower():
            spelling_msg = f"\n\n💡 Kya aap <b>{escape(corrected)}</b> dhundh rahe the? Spelling check karke dobara try karo!"
        # Build reply markup with support button if URL is set
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb_rows = []
        if suggestion_markup:
            kb_rows.extend(suggestion_markup.inline_keyboard)
        if support_url:
            kb_rows.append([InlineKeyboardButton(text="📞 Support / Request Movie", url=support_url)])
        kb_rows.append([InlineKeyboardButton(text="🏠 Home", callback_data="home")])
        final_markup = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
        
        await message.answer(
            f"❌ <b>{escape(query)}</b> naam ki {catalog_label(content_type).lower()} abhi available nahi hai.\n"
            "Movie request karne ke liye neeche Support button dabao."
            + spelling_msg
            + suggestion_text,
            reply_markup=final_markup,
        )
        await log_not_found(message.bot, message.from_user, query, content_type, corrected or "")
        return
    await state.update_data(last_query=query, content_type=content_type)
    top = f"{results[0]['title']} (msg {results[0]['msg_id']})" if results else ""
    await log_search(message.bot, message.from_user, query, len(results), content_type, top)
    await message.answer(
        f"🔎 <b>{escape(query)}</b> — {len(results)} result(s) mile!\n"
        "👇 Quality dekh kar movie select karo:",
        reply_markup=results_kb(results, query),
    )


async def _send_movie_impl(cb: CallbackQuery, bot: Bot, state: FSMContext):
    # Acknowledge immediately so Telegram does not keep the button spinner active.
    await cb.answer("File bhej raha hoon…")
    msg_id = int(cb.data.split(":")[1])
    pending = await not_joined(bot, cb.from_user.id)
    if pending:
        await cb.message.answer(
            "🔒 Required channel leave ho gaya hai. Movie lene ke liye pehle join karo aur Verify dabao.",
            reply_markup=force_join_kb(pending),
        )
        return
    movie = await db.movie(msg_id)
    if not movie:
        await cb.message.answer("⚠️ Ye result ab catalog me available nahi hai. Owner ko /scanall dobara chalana hoga.")
        return
    try:
        src = await get_movie_channel()
    except Exception as exc:
        src = STORAGE_CHANNEL
        logging.exception("Movie source lookup failed: %s", exc)
    delivery_msg_id = int(movie["storage_msg_id"]) if movie["storage_msg_id"] else msg_id
    if delivery_msg_id <= 0:
        await cb.message.answer("⚠️ Is movie ka MAIN_DATA post ID missing hai. Owner ko /scanall dobara chalana hoga.")
        return
    try:
        storage_chat = await bot.get_chat(STORAGE_CHANNEL)
        storage_info = {"id": storage_chat.id, "username": storage_chat.username or ""}
    except Exception:
        storage_info = {"id": STORAGE_CHANNEL, "username": ""}
    link = post_link(storage_info, delivery_msg_id)
    title = movie["title"] if movie else "Movie"
    try:
        caption = await format_movie_caption(bot, movie)
    except Exception as exc:
        logging.exception("Movie caption formatting failed for %s: %s", msg_id, exc)
        caption = f"🎬 <b>{escape(str(title))}</b>"
        try:
            fallback_minutes = int(await db.get("auto_delete_minutes", "0") or 0)
        except (TypeError, ValueError):
            fallback_minutes = 0
        if 1 <= fallback_minutes <= 1440:
            caption += (
                f"\n\n⚠️ <b>Security:</b> Ye content {_expiry_label(fallback_minutes)} me auto-delete hoga. "
                "Movie milte hi save/forward kar lo."
            )

    ok, why = await reserve_quota(cb.from_user.id)
    if not ok:
        await cb.message.answer(why, reply_markup=plans_kb(await db.plans()))
        return

    mode, err = "", ""
    delivered_message_id = 0
    # 1) Primary delivery: original post ko exact MAIN_DATA se direct copy karo.
    # Isse re-upload nahi hota aur MAIN_DATA single source of truth rehta hai.
    try:
        copied = await asyncio.wait_for(
            bot.copy_message(
                cb.from_user.id,
                STORAGE_CHANNEL,
                delivery_msg_id,
                caption=caption[:1024],
                parse_mode="HTML",
            ),
            timeout=8,
        )
        copied_id = getattr(copied, "message_id", 0)
        delivered_message_id = copied_id
        if copied_id and caption:
            try:
                await asyncio.wait_for(
                    bot.edit_message_caption(
                        chat_id=cb.from_user.id,
                        message_id=copied_id,
                        caption=caption[:1024],
                        parse_mode="HTML",
                    ),
                    timeout=5,
                )
            except Exception as caption_error:
                logging.warning("Caption force-edit failed for %s: %s", copied_id, caption_error)
        mode = "copy/main_data_captioned"
        err = ""
    except Exception as e:
        err = str(e)

    # 2) Fallback: deliver through the configured user session. The importer
    # stores tg:<document-id>:<hash> only as a DB dedupe key, not as a Bot API file_id.
    file_id = movie["file_id"] if movie and "file_id" in movie.keys() else ""
    if not mode and file_id.startswith("tg:"):
        try:
            from mtproto_importer import deliver_main_data_to_user
            mtproto_mode = await asyncio.wait_for(
                deliver_main_data_to_user(cb.from_user.id, delivery_msg_id, caption=caption),
                timeout=20,
            )
            if mtproto_mode:
                mode = mtproto_mode
                err = ""
        except Exception as e:
            err = str(e)

    # 3) Fallback: real Bot API file_id from older indexed posts.
    file_type = (movie["file_type"] if movie and "file_type" in movie.keys() else "") or "document"
    if not mode and file_id and not file_id.startswith("tg:"):
        senders = {"video": bot.send_video, "audio": bot.send_audio, "document": bot.send_document}
        try:
            sent = await asyncio.wait_for(
                senders.get(file_type, bot.send_document)(cb.from_user.id, file_id, caption=caption),
                timeout=8,
            )
            delivered_message_id = getattr(sent, "message_id", 0)
            mode = f"file_id/{file_type}"
        except Exception as e:
            err = str(e)

    # 4) Last resort: channel post ka direct button
    if not mode and link:
        mode = "link-fallback"
        fallback_message = await cb.message.answer(
            f"{caption}\n\n👇 File seedha channel post se le lo.",
            reply_markup=movie_link_kb(link),
        )
        delivered_message_id = getattr(fallback_message, "message_id", 0)
    if not mode:
        await refund_quota(cb.from_user.id)
        kind = classify(err)
        mode = f"failed({kind})"
        # The callback was already acknowledged; send the actionable error in chat.
        await cb.message.answer(
            "⚠️ Movie deliver nahi ho payi.\n"
            f"MAIN_DATA post: <code>{delivery_msg_id}</code>\n"
            f"Reason: <code>{escape(err[:500] or 'Telegram delivery failed')}</code>\n"
            "Owner ko LOG_CHANNEL error check karke bot ko MAIN_DATA me admin karna hoga."
        )

    if delivered_message_id and not mode.startswith("failed"):
        try:
            delete_minutes = int(await db.get("auto_delete_minutes", "0"))
        except ValueError:
            delete_minutes = 0
        if 1 <= delete_minutes <= 1440:
            await db.schedule_expiry(
                cb.from_user.id,
                delivered_message_id,
                int(time.time()) + delete_minutes * 60,
            )
            try:
                await cb.message.answer(
                    f"⚠️ <b>Security warning:</b> Ye movie message {_expiry_label(delete_minutes)} me automatically delete ho jayega.\n"
                    "Movie milte hi save ya forward kar lo."
                )
            except Exception as warning_exc:
                logging.warning("Auto-delete warning failed for %s: %s", cb.from_user.id, warning_exc)

    data = await state.get_data()
    query = data.get("last_query", "")
    if not mode.startswith("failed"):
        await db.add_history(cb.from_user.id, query, title, msg_id)
        await db.add_audit(cb.from_user.id, "delivery", f"{title} | mode={mode} | msg={msg_id}")
        # Callback was acknowledged at the beginning; no second answer is needed.

    await log_delivery(bot, cb.from_user, query, str(title), msg_id, mode, err)

    user = await db.user(cb.from_user.id)
    if not is_premium(user) and not mode.startswith("failed"):
        await cb.message.answer(
            "Free limit bachane ke liye Premium le lo ya dosto ko refer karo.",
            reply_markup=plans_kb(await db.plans()),
        )


@router.callback_query(F.data.startswith("directdl:"))
async def direct_download(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """Deliver a movie found via direct channel search (not in local catalog).
    Supports multi-channel: directdl:<channel_id>:<msg_id>"""
    parts = cb.data.split(":")
    try:
        if len(parts) == 3:
            channel_id = int(parts[1])
            msg_id = int(parts[2])
        else:
            channel_id = STORAGE_CHANNEL
            msg_id = int(parts[1])
        if msg_id <= 0:
            raise ValueError("invalid message id")
    except (TypeError, ValueError, IndexError):
        await cb.answer("Invalid movie link", show_alert=True)
        return
    await cb.answer("File bhej raha hoon…")
    pending = await not_joined(bot, cb.from_user.id)
    if pending:
        await cb.message.answer(
            "🔒 Required channel leave ho gaya hai. Movie lene ke liye pehle join karo aur Verify dabao.",
            reply_markup=force_join_kb(pending),
        )
        return
    ok, why = await reserve_quota(cb.from_user.id)
    if not ok:
        await cb.message.answer(why, reply_markup=plans_kb(await db.plans()))
        return
    try:
        caption = "🎬 <b>Movie</b>"
        try:
            delete_minutes = int(await db.get("auto_delete_minutes", "0"))
        except (TypeError, ValueError):
            delete_minutes = 0
        if 1 <= delete_minutes <= 1440:
            caption += (
                f"\n\n⚠️ <b>Security:</b> Ye movie message {_expiry_label(delete_minutes)} me auto-delete hoga. "
                "Movie milte hi save ya forward kar lo."
            )
        copied = await asyncio.wait_for(
            bot.copy_message(
                cb.from_user.id,
                channel_id,
                msg_id,
                caption=caption[:1024],
                parse_mode="HTML",
            ),
            timeout=8,
        )
        copied_id = getattr(copied, "message_id", 0)
        if copied_id and 1 <= delete_minutes <= 1440:
            await db.schedule_expiry(
                cb.from_user.id,
                copied_id,
                int(time.time()) + delete_minutes * 60,
            )
        await db.add_history(cb.from_user.id, "", "Direct channel", msg_id)
    except Exception as exc:
        await refund_quota(cb.from_user.id)
        logging.exception("Direct download failed for %s: %s", msg_id, exc)
        await cb.message.answer(
            "⚠️ Movie deliver nahi ho payi.\n"
            f"Message ID: <code>{msg_id}</code>\n"
            "Owner ko LOG_CHANNEL error check karna hoga."
        )


@router.callback_query(F.data.startswith("send:"))
async def send_movie(cb: CallbackQuery, bot: Bot, state: FSMContext):
    try:
        await _send_movie_impl(cb, bot, state)
    except Exception as exc:
        logging.exception("Unhandled movie delivery failure: %s", exc)
        try:
            await cb.message.answer(
                "⚠️ File send nahi ho payi. Telegram error LOG_CHANNEL me record kar diya gaya hai.\n"
                "Owner ko MAIN_DATA permissions aur TG_SESSION check karna hoga."
            )
        except Exception:
            logging.exception("Could not send delivery error to user")
        if LOG_CHANNEL:
            try:
                await bot.send_message(
                    LOG_CHANNEL,
                    f"❌ <b>Unhandled delivery exception</b>\n"
                    f"User: <code>{cb.from_user.id}</code>\n"
                    f"Callback: <code>{cb.data}</code>\n"
                    f"Error: <code>{escape(str(exc))[:800]}</code>",
                )
            except Exception:
                logging.exception("Could not send unhandled delivery error to LOG_CHANNEL")


@router.callback_query(F.data == "tutorial")
async def tutorial(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await log_callback(bot, cb.from_user, "tutorial", "Viewed tutorial")
    await cb.message.answer("📘 <b>Tutorial</b>\n\n" + await db.get("tutorial"))


@router.callback_query(F.data == "owner_info")
async def owner_info(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    rows = []
    for owner_id in sorted(OWNER_IDS):
        try:
            chat = await bot.get_chat(owner_id)
            label = f"👑 @{chat.username}" if chat.username else f"👑 {chat.full_name}"
            if chat.username:
                rows.append([InlineKeyboardButton(text=label[:64], url=f"https://t.me/{chat.username}")])
            else:
                rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"owner_id:{owner_id}")])
        except Exception:
            rows.append([InlineKeyboardButton(text=f"👑 Owner {owner_id}", callback_data=f"owner_id:{owner_id}")])
    await cb.message.answer(
        "👑 <b>All Owners</b>\nContact any owner below:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows or [[button("No owner configured", callback_data="home")]])
    )


@router.callback_query(F.data.startswith("owner_id:"))
async def owner_id_info(cb: CallbackQuery):
    await cb.answer("Owner username is not public. Use the ID shown in the owner panel.", show_alert=True)


@router.callback_query(F.data == "history")
async def history(cb: CallbackQuery, bot: Bot):
    rows = await db.history(cb.from_user.id)
    await cb.answer()
    await log_callback(bot, cb.from_user, "history", f"Viewed history ({len(rows)} items)")
    if not rows:
        await cb.message.answer("🕘 Abhi koi history nahi hai.")
        return
    lines = [
        f"{i}. {r['title'] or r['query']} — {time.strftime('%d %b %H:%M', time.localtime(r['at']))}"
        for i, r in enumerate(rows, 1)
    ]
    await cb.message.answer("🕘 <b>Last History</b>\n\n" + "\n".join(lines))


@router.callback_query(F.data == "refer")
async def refer(cb: CallbackQuery, bot: Bot):
    me = await bot.get_me()
    user = await db.user(cb.from_user.id)
    rules = "\n".join(f"• {n} referrals → {h} hour premium" for n, h in sorted(REFERRAL_REWARDS.items()))
    await cb.answer()
    referrals = user['referrals'] if user else 0
    await log_referral_view(bot, cb.from_user, referrals)
    await cb.message.answer(
        f"🎁 <b>Refer & Earn</b>\n\nTumhare referrals: <b>{referrals}</b>\n\n{rules}\n\n"
        f"Tumhara link:\nhttps://t.me/{me.username}?start=ref_{cb.from_user.id}"
    )


@router.callback_query(F.data == "online_player")
async def online_player(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await log_callback(bot, cb.from_user, "online_player", "Viewed Watch Online")
    player_bot = await db.get("online_player_bot", ONLINE_PLAYER_BOT)
    if not player_bot:
        await cb.message.answer("🎥 Watch Online abhi owner ne configure nahi kiya. Thodi der baad try karo.")
        return
    await cb.message.answer(
        "🎥 <b>Watch Online</b>\n\n"
        f"1. <code>{escape(player_bot)}</code> ko apne group me add karke admin do.\n"
        "2. Group me voice chat start karo.\n"
        "3. Movie post ko group me forward/tag karke <code>vplay</code> likho.\n"
        "4. Player bot movie ko voice chat me play karega.\n\n"
        "Owner panel se player bot username set karne ke baad ye button users ko exact bot handle dikhayega."
    )


# ---------------- payments ----------------
@router.callback_query(F.data == "plans")
async def plans(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await log_callback(bot, cb.from_user, "plans", "Viewed premium plans")
    rows = await db.plans()
    text = plans_text(rows)
    text += "\n\n✨ Unlimited movies • No daily limit • Fast delivery\n👇 Neeche apna plan choose karo:"
    await cb.message.answer(text, reply_markup=plans_kb(rows))


@router.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery, bot: Bot):
    code = cb.data.split(":")[1]
    plan = await db.plan(code)
    if not plan:
        await cb.answer("Ye plan available nahi hai", show_alert=True)
        return
    qr = await db.get("qr", "")
    upi = await db.get("upi", "")
    cap = (
        f"💳 <b>{plan_label(plan)}</b>\n\n"
        f"{'UPI: <code>' + upi + '</code>' if upi else ''}\n"
        "QR scan karke payment karo, phir screenshot bhejo.\n"
        "Owner approve karte hi premium active ho jayega."
    )
    await cb.answer()
    await log_callback(bot, cb.from_user, f"buy:{code}", f"Selected plan: {plan['label']}")
    if qr:
        await cb.message.answer_photo(qr, caption=cap, reply_markup=paid_kb(code))
    else:
        await cb.message.answer(cap + "\n\n(QR abhi set nahi hua, owner se poochho)", reply_markup=paid_kb(code))


@router.callback_query(F.data.startswith("ss:"))
async def ask_ss(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.set_state(Flow.screenshot)
    await state.update_data(plan=cb.data.split(":")[1])
    await cb.answer()
    await log_callback(bot, cb.from_user, cb.data, "Asked for payment screenshot")
    await cb.message.answer("📸 Payment ka screenshot bhejo (photo).")


@router.message(Flow.screenshot, F.photo)
async def got_ss(message: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    code = data.get("plan", "")
    plan = await db.plan(code)
    if not plan:
        await state.clear()
        await message.answer("❌ Ye payment plan ab available nahi hai. Dobara plan select karo.")
        return
    pid = await db.add_payment(message.from_user.id, code)
    caption = (
        f"💰 <b>New Payment</b>\nPlan: {plan['label']} (₹{plan['price']})\n"
        f"User: {message.from_user.full_name} @{message.from_user.username or 'none'}\n"
        f"ID: <code>{message.from_user.id}</code>\nPayment #{pid}"
    )
    for owner_id in OWNER_IDS:
        try:
            await bot.send_photo(owner_id, message.photo[-1].file_id, caption=caption,
                                 reply_markup=approve_kb(pid, message.from_user.id, code))
        except Exception:
            pass
    if LOG_CHANNEL:
        try:
            await bot.send_photo(LOG_CHANNEL, message.photo[-1].file_id, caption=caption)
        except Exception as exc:
            logging.warning("LOG_CHANNEL payment photo send failed: %s", exc)
    await log_payment(bot, message.from_user, plan['label'], pid)
    await state.clear()
    await message.answer("✅ Screenshot owner ko bhej diya. Approve hote hi premium active ho jayega.")


@router.message(Flow.screenshot)
async def screenshot_non_photo(message: Message, state: FSMContext):
    """If user sends text instead of a photo during payment screenshot state."""
    text = (message.text or "").strip().lower()
    if text in {"/cancel", "cancel"}:
        await state.clear()
        await message.answer("❌ Payment cancelled. Dobara plan select karo.")
        return
    await message.answer("📸 Sirf payment ka <b>screenshot (photo)</b> bhejo.\nCancel karne ke liye <code>/cancel</code> bhejo.")


@router.callback_query(F.data.startswith("appr:"))
async def approve(cb: CallbackQuery, bot: Bot):
    if not await is_owner(cb.from_user.id):
        await cb.answer("Only owner", show_alert=True)
        return
    try:
        prefix, pid, user_id, code = cb.data.split(":")
        if prefix != "appr":
            raise ValueError
        pid, user_id = int(pid), int(user_id)
    except (TypeError, ValueError):
        await cb.answer("Invalid payment callback", show_alert=True)
        return
    plan = await db.plan(code)
    if not plan:
        await cb.answer("Plan missing", show_alert=True)
        return
    until = await db.approve_payment(pid, user_id, plan["days"])
    if until is None:
        await cb.answer("Payment already processed", show_alert=True)
        return
    await cb.answer("Approved ✅")
    await cb.message.edit_reply_markup(reply_markup=None)
    try:
        await bot.send_message(
            user_id,
            f"🎉 Premium activate ho gaya!\nPlan: {plan['label']}\nValid: {human_left(until)}",
        )
    except Exception:
        pass
    await log_payment_approved(bot, cb.from_user.id, user_id, plan['label'], pid)


@router.callback_query(F.data.startswith("rej:"))
async def reject(cb: CallbackQuery, bot: Bot):
    if not await is_owner(cb.from_user.id):
        await cb.answer("Only owner", show_alert=True)
        return
    try:
        prefix, pid, user_id = cb.data.split(":")
        if prefix != "rej":
            raise ValueError
        pid, user_id = int(pid), int(user_id)
    except (TypeError, ValueError):
        await cb.answer("Invalid payment callback", show_alert=True)
        return
    payment = await db.payment(pid)
    if not payment or payment["status"] != "pending":
        await cb.answer("Payment already processed", show_alert=True)
        return
    await db.set_payment_status(pid, "rejected")
    await cb.answer("Rejected")
    await cb.message.edit_reply_markup(reply_markup=None)
    try:
        await bot.send_message(user_id, "❌ Payment verify nahi hua. owner se contact karo.")
    except Exception:
        pass
    await log_payment_rejected(bot, cb.from_user.id, user_id, pid)


@router.callback_query(F.data == "coupon")
async def coupon_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await state.set_state(Flow.coupon)
    await cb.answer()
    await log_callback(bot, cb.from_user, "coupon", "Opened coupon redeem")
    await cb.message.answer("🎟 Coupon code bhejo:")


@router.message(Flow.coupon, F.text)
async def coupon_use(message: Message, bot: Bot, state: FSMContext):
    code = message.text.strip()
    days = await db.redeem_coupon(code, message.from_user.id)
    await state.clear()
    await log_coupon_redeem(bot, message.from_user, code, days is not None, days or 0)
    if days is None:
        await message.answer("❌ Invalid / already used coupon.")
        return
    until = await db.add_premium(message.from_user.id, days)
    await message.answer(f"✅ Coupon applied! {days} din premium. Valid: {human_left(until)}")


@router.message(Command("myplan"))
async def myplan(message: Message, bot: Bot):
    await log_command(bot, message.from_user, "myplan")
    user = await db.user(message.from_user.id)
    if is_premium(user):
        await message.answer(f"💎 Premium active — {human_left(user['premium_until'])} bacha hai.")
    else:
        await message.answer("🆓 Free plan. /start se Premium le sakte ho.")


@router.message(Command("request"))
async def request_command(message: Message, bot: Bot):
    """Formal movie request command — logs to LOG_CHANNEL for owner visibility."""
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("📝 Movie request karne ke liye: <code>/request Movie Name</code>")
        return
    await log_movie_request(bot, message.from_user, query)
    await db.add_audit(message.from_user.id, "request", query)
    support_url = await db.get("support_url", "") or SUPPORT_URL
    text = (
        f"✅ Tumhara request <b>{escape(query)}</b> owner ko bhej diya gaya hai.\n"
        "Jaise hi ye movie catalog me add hogi, tumhe notify kar diya jayega."
    )
    if support_url:
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📞 Support Group", url=support_url)],
            [InlineKeyboardButton(text="🏠 Home", callback_data="home")],
        ]))
    else:
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Home", callback_data="home")],
        ]))


@router.message(Command("stats"))
async def stats_command(message: Message, bot: Bot):
    """Show user their personal usage stats."""
    await log_command(bot, message.from_user, "stats")
    user = await db.user(message.from_user.id)
    if not user:
        await message.answer("Pehle /start dabao.")
        return
    history = await db.history(message.from_user.id)
    total_catalog = await db.movie_count(storage_only=True)
    premium_status = f"💎 Premium ({human_left(user['premium_until'])})" if is_premium(user) else "🆓 Free"
    import datetime as _dt
    d = _dt.date.today().isoformat()
    used_today = user["free_used_today"] if user["last_free_day"] == d else 0
    from config import FIRST_DAY_FREE, DAILY_FREE
    limit = FIRST_DAY_FREE if is_first_calendar_day(user) else DAILY_FREE
    text = (
        f"📊 <b>Your Stats</b>\n\n"
        f"👤 <b>Name:</b> {escape(message.from_user.full_name)}\n"
        f"🆔 <b>ID:</b> <code>{message.from_user.id}</code>\n"
        f"📋 <b>Plan:</b> {premium_status}\n"
        f"🎬 <b>Movies watched:</b> {len(history)}\n"
        f"📅 <b>Today's usage:</b> {used_today}/{limit if not is_premium(user) else '∞'}\n"
        f"👥 <b>Referrals:</b> {user['referrals']}\n"
        f"📚 <b>Total catalog:</b> {total_catalog} movies\n"
    )
    await message.answer(text)


@router.callback_query(F.data == "trending")
async def trending_movies(cb: CallbackQuery, bot: Bot):
    """Show most popular movies based on delivery history."""
    await cb.answer()
    await log_callback(bot, cb.from_user, "trending", "Viewed trending movies")
    movies = await db.popular_movies("movie", 30)
    if not movies:
        await cb.message.answer("🔥 Abhi koi trending data nahi hai. Movies dekhne par yahan dikhengi.")
        return
    text = f"🔥 <b>Trending Movies</b>\n\nTop {len(movies)} most watched movies:\nTap karo aur seedha le lo 👇"
    await cb.message.answer(text, reply_markup=browse_kb(movies, page=0, callback_prefix="trending"))


@router.callback_query(F.data.startswith("trending:"))
async def trending_page(cb: CallbackQuery, bot: Bot):
    """Pagination for trending movies."""
    await cb.answer()
    page = int(cb.data.split(":")[1])
    movies = await db.popular_movies("movie", 30)
    await cb.message.edit_text(
        f"🔥 <b>Trending Movies</b>\n\nPage {page + 1}\nTap karo aur seedha le lo 👇",
        reply_markup=browse_kb(movies, page=page, callback_prefix="trending"),
    )


@router.callback_query(F.data == "recent")
async def recent_movies_cb(cb: CallbackQuery, bot: Bot):
    """Show recently added movies from catalog."""
    await cb.answer()
    await log_callback(bot, cb.from_user, "recent", "Viewed recently added movies")
    movies = await db.recent_movies("movie", 30, storage_only=True)
    if not movies:
        await cb.message.answer("🆕 Abhi catalog me koi movie nahi hai. Owner ko /scanall chalane ko bolo.")
        return
    text = f"🆕 <b>Recently Added Movies</b>\n\nLatest {len(movies)} movies from catalog:\nTap karo aur seedha le lo 👇"
    await cb.message.answer(text, reply_markup=browse_kb(movies, page=0, callback_prefix="recent"))


@router.callback_query(F.data.startswith("recent:"))
async def recent_page(cb: CallbackQuery, bot: Bot):
    """Pagination for recently added movies."""
    await cb.answer()
    page = int(cb.data.split(":")[1])
    movies = await db.recent_movies("movie", 30, storage_only=True)
    await cb.message.edit_text(
        f"🆕 <b>Recently Added Movies</b>\n\nPage {page + 1}\nTap karo aur seedha le lo 👇",
        reply_markup=browse_kb(movies, page=page, callback_prefix="recent"),
    )
