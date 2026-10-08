import asyncio
import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import LOG_CHANNEL, MAIN_DATA, ONLINE_PLAYER_BOT, OWNER_IDS, SCAN_START_ID, STORAGE_CHANNEL
from log_helper import log_owner_action, log_broadcast, log_command
from transfer_manager import transfer_manager
from helper_pool import helper_pool
from db import db
from keyboards import back_panel, button, owner_panel_kb, plans_kb, plans_text
from indexer import ScanStats, scan_ids, scan_range, scan_channel_fast
from snapshot import SNAPSHOT_MARKER, request_snapshot
from utils import get_movie_channel, human_left, is_premium, resolve_channel, user_tag

router = Router()
_scanmain_lock = asyncio.Lock()


class Own(StatesGroup):
    startpic = State()
    qr = State()
    price = State()
    gift = State()
    coupon = State()
    broadcast = State()
    admin = State()
    force = State()
    caption = State()
    autodelete = State()
    scan = State()
    setchannel = State()
    tutorial = State()
    online = State()


async def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


@router.message(Command("ownerpanel"))
async def panel_cmd(message: Message, bot: Bot):
    if not await is_owner(message.from_user.id):
        return
    await log_command(bot, message.from_user, "ownerpanel")
    await message.answer("🛠 <b>Owner Panel</b>", reply_markup=owner_panel_kb())


@router.callback_query(F.data == "op:home")
async def panel(cb: CallbackQuery, state: FSMContext):
    if not await is_owner(cb.from_user.id):
        await cb.answer("Only owner", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    await cb.message.answer("🛠 <b>Owner Panel</b>", reply_markup=owner_panel_kb())


async def _ask(cb: CallbackQuery, state: FSMContext, st: State, text: str):
    if not await is_owner(cb.from_user.id):
        await cb.answer("Only owner", show_alert=True)
        return False
    await state.set_state(st)
    await cb.answer()
    await cb.message.answer(text, reply_markup=back_panel())
    return True


@router.callback_query(F.data == "op:startpic")
async def op_startpic(cb: CallbackQuery, state: FSMContext):
    await _ask(cb, state, Own.startpic,
               "🖼 Start pic ka <b>GitHub raw link</b> bhejo (ya photo bhej do).")


@router.message(Own.startpic)
async def set_startpic(message: Message, bot: Bot, state: FSMContext):
    value = (message.text or "").strip()
    if message.photo:
        try:
            saved = await bot.forward_message(STORAGE_CHANNEL, message.chat.id, message.message_id, disable_notification=True)
            value = saved.photo[-1].file_id
        except Exception as exc:
            return await message.answer(f"❌ Start pic storage me save nahi hui: <code>{str(exc)[:160]}</code>")
    if not value:
        return await message.answer("GitHub/raw image URL ya photo bhejo.")
    await db.set("start_pic", value)
    await state.clear()
    await message.answer("✅ Start pic durable storage me save ho gayi.", reply_markup=owner_panel_kb())


@router.callback_query(F.data == "op:qr")
async def op_qr(cb: CallbackQuery, state: FSMContext):
    await _ask(cb, state, Own.qr,
               "💳 Payment QR ka link bhejo ya QR photo bhejo.\n"
               "UPI id set karni ho to likho: <code>upi:yourid@bank</code>")


@router.message(Own.qr)
async def set_qr(message: Message, state: FSMContext):
    if message.photo:
        await db.set("qr", message.photo[-1].file_id)
    else:
        text = (message.text or "").strip()
        if text.lower().startswith("upi:"):
            await db.set("upi", text[4:].strip())
        else:
            await db.set("qr", text)
    await state.clear()
    await message.answer("✅ Payment details update ho gayi.", reply_markup=owner_panel_kb())


def _norm_plan_key(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def parse_plan_price_lines(text: str, plans: list) -> tuple[list[tuple[str, int, str | None]], list[str]]:
    """Parse owner input into (code, price, tag_or_None) updates.

    Accepts one line or the whole plan list at once, with or without ₹ and
    badges:  `1month 149`, `📅 1 Month — ₹149 — ⭐ BEST VALUE`, `7day 69 POPULAR`.
    """
    by_key: dict[str, str] = {}
    for plan in plans:
        by_key[_norm_plan_key(plan["code"])] = plan["code"]
        by_key[_norm_plan_key(plan["label"])] = plan["code"]
    updates: list[tuple[str, int, str | None]] = []
    bad: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cleaned = line.replace("₹", " ").replace("—", " ").replace("–", " ")
        tokens = [t for t in cleaned.split() if t]
        digit_positions = [i for i, t in enumerate(tokens) if t.isdigit() and i > 0]
        code = None
        price_at = -1
        # Try the right-most number first: that handles both `1month 149` and a
        # pasted line like `📅 1 Month — ₹149 — ⭐ BEST VALUE`.
        for index in reversed(digit_positions):
            name = " ".join(tokens[:index])
            candidate = by_key.get(_norm_plan_key(name))
            if not candidate:
                for token in tokens[:index]:
                    candidate = by_key.get(_norm_plan_key(token))
                    if candidate:
                        break
            if candidate:
                code, price_at = candidate, index
                break
        if not code:
            if any(ch.isdigit() for ch in line):
                bad.append(line)
            continue
        try:
            price = int(tokens[price_at])
        except ValueError:
            bad.append(line)
            continue
        if price < 0:
            bad.append(line)
            continue
        tail = " ".join(tokens[price_at + 1:]).strip(" -–—")
        updates.append((code, price, tail if tail else None))
    return updates, bad


@router.callback_query(F.data == "op:price")
async def op_price(cb: CallbackQuery, state: FSMContext):
    rows = await db.plans()
    codes = "\n".join(f"<code>{p['code']}</code> = {p['label']}" for p in rows)
    await _ask(cb, state, Own.price,
               plans_text(rows) + "\n\n"
               "✏️ <b>Naya price bhejo</b>\n"
               "• Ek plan: <code>1month 149</code>\n"
               "• Sabhi ek saath: har line me ek plan\n"
               "• Badge bhi set karna ho: <code>7day 69 POPULAR</code>\n"
               "• Badge hatana ho: <code>7day 69 -</code>\n\n"
               f"<b>Plan codes</b>\n{codes}")


@router.message(Own.price)
async def set_price(message: Message, state: FSMContext, bot: Bot = None):
    rows = await db.plans()
    updates, bad = parse_plan_price_lines(message.text or "", rows)
    if not updates:
        await message.answer(
            "❌ Kuch samajh nahi aaya. Example: <code>1month 149</code>\n"
            "Poori list bhi paste kar sakte ho — har line me ek plan."
        )
        return
    for code, price, tag in updates:
        await db.set_plan_price(code, price)
        if tag is not None:
            await db.set_plan_tag(code, "" if tag.strip() in {"-", "none", "remove"} else tag)
    await state.clear()
    rows = await db.plans()
    if bot:
        await log_owner_action(bot, message.from_user.id, "set_price", f"{len(updates)} plans updated")
    note = f"✅ <b>{len(updates)} plan update ho gaye.</b> Users ko bilkul yahi list dikhegi:\n\n"
    if bad:
        note += "⚠️ Ye line samajh nahi aayi: <code>" + "</code>, <code>".join(b[:40] for b in bad) + "</code>\n\n"
    await message.answer(note + plans_text(rows), reply_markup=plans_kb(rows))
    await message.answer("⬅️ Owner panel", reply_markup=owner_panel_kb())


@router.callback_query(F.data == "op:gift")
async def op_gift(cb: CallbackQuery, state: FSMContext):
    await _ask(cb, state, Own.gift,
               "🎁 Gift premium — format: <code>userid days</code>\nExample: <code>123456789 30</code>")


@router.message(Own.gift)
async def do_gift(message: Message, bot: Bot, state: FSMContext):
    try:
        uid, days = (message.text or "").split()
        uid, days = int(uid), float(days)
        if uid <= 0 or days <= 0:
            raise ValueError
    except Exception:
        await message.answer("Format: <code>userid days</code>")
        return
    if not await db.user(uid):
        await db.add_user(uid, "", "gifted")
    until = await db.add_premium(uid, days)
    await state.clear()
    await log_owner_action(bot, message.from_user.id, "gift_premium", f"User {uid}: {days} days")
    await message.answer(f"✅ {uid} ko {days} din premium. Valid: {human_left(until)}",
                         reply_markup=owner_panel_kb())
    try:
        await bot.send_message(uid, f"🎁 Owner ne tumhe {days} din ka premium gift kiya!")
    except Exception:
        pass


@router.callback_query(F.data == "op:coupon")
async def op_coupon(cb: CallbackQuery, state: FSMContext):
    await _ask(cb, state, Own.coupon,
               "🎟 Coupon banao — format: <code>CODE days max_uses</code>\nExample: <code>ROHAN50 7 25</code>")


@router.message(Own.coupon)
async def do_coupon(message: Message, bot: Bot, state: FSMContext):
    try:
        code, days, uses = (message.text or "").split()
        days, uses = int(days), int(uses)
        if not code.strip() or days <= 0 or uses <= 0:
            raise ValueError
        await db.add_coupon(code, days, uses)
    except Exception:
        await message.answer("Format: <code>CODE days max_uses</code>")
        return
    await state.clear()
    await log_owner_action(bot, message.from_user.id, "create_coupon", f"{code.upper()}: {days}d/{uses}uses")
    await message.answer(f"✅ Coupon <b>{code.upper()}</b> ready.", reply_markup=owner_panel_kb())


@router.callback_query(F.data == "op:premlist")
async def op_premlist(cb: CallbackQuery):
    if not await is_owner(cb.from_user.id):
        return await cb.answer("Only owner", show_alert=True)
    rows = await db.premium_users()
    await cb.answer()
    if not rows:
        return await cb.message.answer("Koi premium user nahi hai.", reply_markup=back_panel())
    text = "\n".join(
        f"{i}. {r['name']} (@{r['username'] or '-'}) <code>{r['user_id']}</code> — {human_left(r['premium_until'])}"
        for i, r in enumerate(rows[:50], 1)
    )
    await cb.message.answer(f"💎 <b>Premium Users ({len(rows)})</b>\n\n{text}", reply_markup=back_panel())


@router.callback_query(F.data == "op:stats")
async def op_stats(cb: CallbackQuery):
    if not await is_owner(cb.from_user.id):
        return await cb.answer("Only owner", show_alert=True)
    prem = await db.premium_users()
    await cb.answer()
    await cb.message.answer(
        f"📊 <b>Stats</b>\nRegistered users: <b>{await db.user_count()}</b>\nPremium users: <b>{len(prem)}</b>\nTotal catalog movies: <b>{await db.movie_count()}</b>\nMAIN_DATA-linked movies: <b>{await db.movie_count(storage_only=True)}</b>",
        reply_markup=back_panel(),
    )


@router.callback_query(F.data == "op:admin")
async def op_admin(cb: CallbackQuery, state: FSMContext):
    admins = await db.admins()
    await _ask(cb, state, Own.admin,
               f"👮 Admins: {admins or 'koi nahi'}\n\n"
               "Add: <code>add 123456789</code>\nRemove: <code>del 123456789</code>")


@router.message(Own.admin)
async def do_admin(message: Message, bot: Bot, state: FSMContext):
    try:
        action, uid = (message.text or "").split()
        uid = int(uid)
    except Exception:
        return await message.answer("Format: <code>add 123456789</code>")
    if action == "add":
        await db.add_admin(uid)
    elif action == "del":
        await db.remove_admin(uid)
    else:
        return await message.answer("Action sirf <code>add</code> ya <code>del</code> ho sakta hai.")
    await state.clear()
    await log_owner_action(bot, message.from_user.id, f"admin_{action}", str(uid))
    await message.answer("✅ Done.", reply_markup=owner_panel_kb())


@router.callback_query(F.data == "op:caption")
async def op_caption(cb: CallbackQuery, state: FSMContext):
    current = await db.get("caption_template", "")
    await _ask(cb, state, Own.caption,
               "📝 <b>Caption settings</b>\n\n"
               "Full template bhejo, ya sirf line change karo:\n"
               "<code>join:https://t.me/YourChannel</code>\n<code>visit:https://t.me/YourUsername</code>\n"
               "Remove ke liye <code>join:remove</code> / <code>visit:remove</code>; Heroku default ke liye <code>join:reset</code> / <code>visit:reset</code>.\n\n"
               "Placeholders: <code>{movie_name}</code> <code>{quality}</code> <code>{duration}</code> <code>{join}</code> <code>{username}</code>\n\n"
               f"Current:\n<code>{current[:900]}</code>")


@router.callback_query(F.data == "op:autodelete")
async def op_autodelete(cb: CallbackQuery, state: FSMContext):
    current = await db.get("auto_delete_minutes", "0")
    await _ask(
        cb, state, Own.autodelete,
        f"🗑 <b>Auto Delete</b>\n\nCurrent: <code>{current} minutes</code>\n\n"
        "Delivered movie messages ko automatically delete karne ka time bhejo. "
        "Allowed: <code>1</code> se <code>1440</code> minutes (24 hours).\n"
        "Disable karne ke liye <code>0</code> bhejo.\n\n"
        "User ko message milte hi save/forward karne ka time milega."
    )


@router.message(Own.autodelete)
async def set_autodelete(message: Message, state: FSMContext):
    try:
        minutes = int((message.text or "").strip())
    except ValueError:
        return await message.answer("Sirf minutes bhejo: <code>1</code>–<code>1440</code>, ya disable ke liye <code>0</code>.")
    if minutes < 0 or minutes > 1440:
        return await message.answer("Time <code>0</code> ya <code>1</code>–<code>1440</code> minutes hona chahiye.")
    await db.set("auto_delete_minutes", str(minutes))
    await state.clear()
    status = "disabled" if minutes == 0 else f"{minutes} minutes"
    await message.answer(f"✅ Auto-delete {status} set ho gaya.", reply_markup=owner_panel_kb())


@router.message(Own.caption)
async def set_caption(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value:
        return await message.answer("Caption text bhejo.")
    low = value.lower()
    if low.startswith("join:"):
        link = value[5:].strip()
        await db.set("caption_join", "-" if link.lower() in {"remove", "off", "disable"} else "" if link.lower() == "reset" else link)
    elif low.startswith("visit:"):
        link = value[6:].strip()
        await db.set("caption_visit", "-" if link.lower() in {"remove", "off", "disable"} else "" if link.lower() == "reset" else link)
    else:
        allowed = {"{movie_name}", "{quality}", "{duration}", "{join}", "{username}", "{extra}"}
        if not any(token in value for token in allowed):
            return await message.answer("Template me kam se kam ek placeholder use karo.")
        await db.set("caption_template", value)
    await state.clear()
    await message.answer("✅ Caption setting update ho gayi.", reply_markup=owner_panel_kb())


def _verify_target(raw: str) -> str:
    """Accept public usernames, numeric IDs, AND private invite links."""
    value = raw.strip().rstrip("/")
    value = re.sub(r"^https?://t\.me/", "", value, flags=re.I)
    # Private invite links: +abc123 or joinchat/abc123
    if value.startswith("+") or value.lower().startswith("joinchat/"):
        # Return the full invite link format for private channels
        invite_code = value.lstrip("+")
        if value.lower().startswith("joinchat/"):
            invite_code = value[9:]
        return f"https://t.me/+{invite_code}"
    if "/" in value:
        value = value.split("/", 1)[0]
    if value and not value.startswith("@") and not value.lstrip("-").isdigit():
        value = "@" + value
    return value


@router.callback_query(F.data == "op:force")
async def op_force(cb: CallbackQuery, state: FSMContext):
    cur = await db.get("force_channels", "")
    current = ", ".join(x for x in cur.split(",") if x.strip()) or "none"
    await _ask(cb, state, Own.force,
               f"🔗 <b>Verify URLs / channels</b> — max 5\nAbhi: <code>{current}</code>\n\n"
               "Add: <code>add @channel</code> ya <code>add https://t.me/channel</code>\n"
               "Private: <code>add https://t.me/+abc123</code> ya <code>add -1001234567890</code>\n"
               "Remove: <code>del @channel</code>\nReplace all: <code>set @one,@two</code>\n"
               "Public username, numeric chat ID, ya private invite link — sab supported hain.")


@router.message(Own.force)
async def do_force(message: Message, bot: Bot, state: FSMContext):
    raw = (message.text or "").strip()
    current = [_verify_target(x) for x in (await db.get("force_channels", "")).split(",") if _verify_target(x)]
    parts = raw.split(maxsplit=1)
    action = parts[0].lower() if parts else ""
    if action in {"add", "del", "remove", "set"}:
        payload = parts[1] if len(parts) > 1 else ""
        values = [_verify_target(x) for x in payload.split(",") if _verify_target(x)]
        if action == "add":
            for value in values:
                if value not in current:
                    current.append(value)
        elif action in {"del", "remove"}:
            current = [value for value in current if value not in set(values)]
        else:
            current = values
    else:
        current = [_verify_target(x) for x in raw.split(",") if _verify_target(x)]
    if len(current) > 5:
        return await message.answer("❌ Maximum 5 verify URLs/channels allowed. Pehle kuch remove karo.")
    if not current and raw and action not in {"del", "remove", "set"}:
        return await message.answer("❌ Valid public channel username/ID nahi mila.")
    if action in {"add", "set"} and payload and not values:
        return await message.answer("❌ Valid public channel username/ID nahi mila.")
    await db.set("force_channels", ",".join(current))
    await state.clear()
    await log_owner_action(bot, message.from_user.id, "set_force_channels", ", ".join(current) or "none")
    await message.answer(
        f"✅ Verify list update ho gayi ({len(current)}/5).\nCurrent: <code>{', '.join(current) or 'none'}</code>\n"
        "Private links ke liye bot ko channel me add karna padega, phir membership check work karega.",
        reply_markup=owner_panel_kb())


@router.callback_query(F.data == "op:tutorial")
async def op_tutorial(cb: CallbackQuery, state: FSMContext):
    current = await db.get("tutorial", "")
    await _ask(cb, state, Own.tutorial,
               "📘 Clean Hinglish tutorial bhejo. Multi-line text allowed hai.\n\n"
               f"Current:\n<code>{current[:1800]}</code>")


@router.message(Own.tutorial)
async def set_tutorial(message: Message, state: FSMContext):
    value = (message.text or message.caption or "").strip()
    if len(value) < 20:
        return await message.answer("Tutorial thoda detailed bhejo (kam se kam 20 characters).")
    await db.set("tutorial", value[:3800])
    await state.clear()
    await message.answer("✅ Tutorial save ho gaya.", reply_markup=owner_panel_kb())


@router.callback_query(F.data == "op:online")
async def op_online(cb: CallbackQuery, state: FSMContext):
    current = await db.get("online_player_bot", ONLINE_PLAYER_BOT)
    await _ask(cb, state, Own.online,
               f"🎥 Watch Online ke player bot ka username bhejo.\nCurrent: <code>{current or 'not set'}</code>\n"
               "Example: <code>@MyPlayerBot</code>\nDisable: <code>off</code>")


@router.message(Own.online)
async def set_online(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if value.lower() in {"off", "disable", "remove"}:
        value = ""
    elif value:
        value = re.sub(r"^https?://t\.me/", "", value, flags=re.I).strip("/")
        value = "@" + value.lstrip("@")
    if value and (" " in value or len(value) > 64):
        return await message.answer("Valid Telegram bot username bhejo, example: <code>@MyPlayerBot</code>.")
    await db.set("online_player_bot", value)
    await state.clear()
    await message.answer(f"✅ Watch Online bot {'disable' if not value else 'set: ' + value}.", reply_markup=owner_panel_kb())


@router.callback_query(F.data == "op:broadcast")
async def op_broadcast(cb: CallbackQuery, state: FSMContext):
    await _ask(cb, state, Own.broadcast,
               "📢 Jo message broadcast karna hai wo bhejo (text/photo/video/document/album).\n\n"
               "Message <b>forward format</b> me hi jayegi — original sender, channel, media sab "
               "original ke saath users ko bhej di jayegi. Album/media-group bhi support hai.")


@router.message(Own.broadcast)
async def do_broadcast(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    users = await db.all_user_ids()

    # Collect all message IDs to forward. If the owner sent an album (media
    # group), forward every item so the whole group reaches each user intact.
    forward_ids = [message.message_id]
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        try:
            history = await bot.get_chat_history(message.chat.id, limit=20)
            forward_ids = [
                m.message_id for m in history
                if getattr(m, "media_group_id", None) == media_group_id
            ]
            forward_ids.sort()
        except Exception:
            forward_ids = [message.message_id]

    note = await message.answer(
        f"📢 Forwarding to {len(users)} users"
        f" ({len(forward_ids)} message{'s' if len(forward_ids) > 1 else ''})..."
    )
    await log_owner_action(bot, message.from_user.id, "broadcast_start",
                           f"To {len(users)} users, {len(forward_ids)} msgs")

    # Test one forward first to detect protected content. If the source channel
    # has "Restrict saving content" ON, forward_message will fail — we switch to
    # copy_message so the broadcast still goes out to every user.
    use_copy = False
    try:
        test_fwd = await bot.forward_message(
            chat_id=message.from_user.id,
            from_chat_id=message.chat.id,
            message_id=forward_ids[0],
        )
        try:
            await bot.delete_message(message.from_user.id, test_fwd.message_id)
        except Exception:
            pass
    except Exception as exc:
        low = str(exc).lower()
        if "forward" in low and ("restrict" in low or "protected" in low or "can't" in low):
            use_copy = True
            logging.info("Broadcast: source is forward-restricted, using copy_message")

    limiter = asyncio.Semaphore(15)

    permanent_errors = 0
    protected_errors = 0

    async def deliver(uid: int) -> bool:
        nonlocal permanent_errors, protected_errors
        async with limiter:
            for attempt in range(2):
                last_exc = None
                success = True
                for mid in forward_ids:
                    try:
                        if use_copy:
                            await bot.copy_message(
                                chat_id=uid,
                                from_chat_id=message.chat.id,
                                message_id=mid,
                            )
                        else:
                            await bot.forward_message(
                                chat_id=uid,
                                from_chat_id=message.chat.id,
                                message_id=mid,
                            )
                    except Exception as exc:
                        low = str(exc).lower()
                        if "can't be forwarded" in low or "chat_forwards_restricted" in low or ("forward" in low and "restrict" in low):
                            # Try copy as fallback for this specific user
                            try:
                                await bot.copy_message(
                                    chat_id=uid,
                                    from_chat_id=message.chat.id,
                                    message_id=mid,
                                )
                                continue
                            except Exception:
                                protected_errors += 1
                                return False
                        if "forbidden" in low or "blocked" in low or "user is deactivated" in low:
                            permanent_errors += 1
                            return False
                        last_exc = exc
                        success = False
                        break
                if success:
                    return True
                if attempt == 0 and last_exc is not None:
                    low = str(last_exc).lower()
                    if "retry after" in low:
                        m = re.search(r"retry after (\d+)", low)
                        await asyncio.sleep(int(m.group(1)) + 1 if m else 1)
                    else:
                        await asyncio.sleep(1)
                    continue
                return False
            return False

    results = await asyncio.gather(*(deliver(uid) for uid in users))
    sent = sum(results)
    failed = len(results) - sent
    method_label = "Copy" if use_copy else "Forward"
    summary = f"✅ {method_label} broadcast done.\nDelivered: {sent}/{len(users)}\nFailed: {failed}"
    if protected_errors:
        summary += f"\n🔒 {protected_errors} user(s) ko forward nahi hua — source content protected hai."
    await note.edit_text(summary)
    await log_broadcast(bot, message.from_user.id, len(users), sent, failed)


@router.callback_query(F.data == "op:setchannel")
async def op_setchannel(cb: CallbackQuery, state: FSMContext):
    cur = await get_movie_channel()
    await _ask(cb, state, Own.setchannel,
               f"🎞 <b>Movie channels set</b>\n\nAbhi: <code>{await db.get('movie_channels', str(cur or 'not set'))}</code>\n\n"
               "Sirf source chat ID/username bhejo; save hone ke baad `/scanall` ya `/scanfull` manually chalao:\n"
               "<code>-1003906081303</code>\nBot source channel aur MAIN_DATA me admin hona chahiye; unique media ki copy MAIN_DATA me save hogi.")


@router.message(Own.setchannel, ~F.text.startswith("/"))
async def do_setchannel(message: Message, bot: Bot, state: FSMContext):
    values = [v.strip() for v in (message.text or "").split(",") if v.strip()]
    if not values:
        return await message.answer("Kam se kam ek channel ID/username bhejo.")
    ids = []
    titles = []
    for val in values:
        try:
            chat = await bot.get_chat(int(val) if val.lstrip("-").isdigit() else val)
        except Exception as e:
            return await message.answer(f"❌ Channel {val} nahi khula: <code>{e}</code>")
        ids.append(str(chat.id))
        titles.append(chat.title or val)
    await db.set("movie_channels", ",".join(ids))
    await db.set("movie_channel", ids[0])
    await state.clear()
    await message.answer(
        f"✅ {len(ids)} source channel(s) save ho gaye: <b>{', '.join(titles[:5])}</b>\n\n"
        "Ab scan automatically start nahi hoga. Owner Panel → Scan Movie Channel se "
        "<code>/scanall</code> ya apni range bhejkar MAIN_DATA me index/copy banao."
    )


async def _clean_main_data_duplicates_actual(bot: Bot, start_id: int, end_id: int, progress=None) -> tuple[int, int, int]:
    """Inspect real MAIN_DATA history, then delete older same-caption posts."""
    from mtproto_importer import find_duplicate_caption_ids
    scanned, skipped, duplicate_ids = await find_duplicate_caption_ids(
        MAIN_DATA, min_id=end_id, max_id=start_id
    )
    delete_sem = asyncio.Semaphore(24)

    async def delete_post(mid: int) -> bool:
        async with delete_sem:
            try:
                await bot.delete_message(MAIN_DATA, mid)
            except Exception as exc:
                text = str(exc).lower()
                if "not found" not in text and "message_id_invalid" not in text:
                    logging.warning("Could not delete duplicate MAIN_DATA/%s: %s", mid, exc)
                    return False
            await db.delete_movie_by_storage_id(mid)
            return True

    deleted = 0
    for offset in range(0, len(duplicate_ids), 500):
        batch = duplicate_ids[offset:offset + 500]
        deleted += sum(await asyncio.gather(*(delete_post(mid) for mid in batch)))
        if progress:
            await progress(scanned, deleted, skipped)
    if progress:
        await progress(scanned, deleted, skipped)
    return scanned, deleted, skipped


async def _clean_main_data_duplicates_fast(bot: Bot, start_id: int, end_id: int, progress=None) -> tuple[int, int, int]:
    """Delete older same-caption MAIN_DATA posts using the local catalog index."""
    rows = await db.all_movies(storage_only=True)
    groups = {}
    scanned = skipped = 0
    for row in rows:
        storage_id = int(row["storage_msg_id"] or 0)
        if not (end_id <= storage_id <= start_id):
            continue
        scanned += 1
        caption_key = re.sub(r"\s+", " ", (row["caption"] or "").strip()).casefold()
        if not caption_key:
            skipped += 1
            continue
        groups.setdefault(caption_key, []).append(storage_id)
    duplicate_ids = [mid for ids in groups.values() for mid in sorted(ids, reverse=True)[1:]]
    delete_sem = asyncio.Semaphore(24)

    async def delete_post(mid: int) -> bool:
        async with delete_sem:
            try:
                await bot.delete_message(MAIN_DATA, mid)
            except Exception as exc:
                # If the post is already gone, stale local rows can be removed.
                if "not found" not in str(exc).lower() and "message_id_invalid" not in str(exc).lower():
                    logging.warning("Could not delete duplicate MAIN_DATA/%s: %s", mid, exc)
                    return False
            await db.delete_movie_by_storage_id(mid)
            return True

    deleted = 0
    for offset in range(0, len(duplicate_ids), 500):
        batch = duplicate_ids[offset:offset + 500]
        deleted += sum(await asyncio.gather(*(delete_post(mid) for mid in batch)))
        if progress:
            await progress(scanned, deleted, skipped)
    if progress:
        await progress(scanned, deleted, skipped)
    return scanned, deleted, skipped


@router.message(Command("clean"))
@router.message(Command("clean_duplicates"))
async def clean_duplicates_command(message: Message, bot: Bot):
    if not await is_owner(message.from_user.id):
        return
    await log_command(bot, message.from_user, "clean_duplicates")
    parts = (message.text or "").split()[1:]
    try:
        if not parts or parts[0].lower() == "full":
            start_id, end_id = SCAN_START_ID, 1
        elif len(parts) == 2:
            start_id, end_id = int(parts[0]), int(parts[1])
        else:
            raise ValueError
    except ValueError:
        return await message.answer(
            "Format: <code>/clean_duplicates start_id end_id</code>\n"
            "Example: <code>/clean_duplicates 10000 1</code>\n"
            "Complete history ke liye: <code>/clean_duplicates full</code>"
        )
    if start_id <= 0 or end_id <= 0 or start_id < end_id:
        return await message.answer("Newest message ID pehle aur oldest ID baad me do. Example: <code>/clean_duplicates 10000 1</code>")
    note = await message.answer(
        f"🧹 MAIN_DATA duplicate cleanup start: <code>{start_id} → {end_id}</code>\n"
        "Same normalized caption wali purani posts delete hongi; newest post rakhi jayegi. Empty-caption posts safe rahengi."
    )

    async def progress(scanned: int, deleted: int, skipped: int) -> None:
        try:
            await note.edit_text(
                f"🧹 <b>Cleaning MAIN_DATA</b>\n"
                f"Checked: {scanned} | Deleted duplicates: {deleted} | Empty/unavailable: {skipped}"
            )
        except Exception:
            pass

    started = time.time()
    heartbeat = None

    async def log_heartbeat() -> None:
        while True:
            await asyncio.sleep(60)
            if LOG_CHANNEL:
                try:
                    elapsed = int(time.time() - started)
                    await bot.send_message(
                        LOG_CHANNEL,
                        f"🧹 <b>MAIN_DATA cleanup progress</b>\n"
                        f"Range: <code>{start_id} → {end_id}</code>\n"
                        "Cleanup abhi chal raha hai; latest progress owner panel me dekh sakte ho.\n"
                        f"Elapsed: <b>{elapsed}s</b>",
                    )
                except Exception as exc:
                    logging.warning("Cleanup progress log failed: %s", exc)

    heartbeat = asyncio.create_task(log_heartbeat(), name="duplicate-cleanup-heartbeat")
    try:
        try:
            from config import TG_API_HASH, TG_API_ID, TG_SESSION
            use_actual_history = bool(TG_API_ID and TG_API_HASH and TG_SESSION)
        except Exception:
            use_actual_history = False
        cleaner = _clean_main_data_duplicates_actual if use_actual_history else _clean_main_data_duplicates_fast
        scanned, deleted, skipped = await cleaner(bot, start_id, end_id, progress)
    finally:
        if heartbeat and not heartbeat.done():
            heartbeat.cancel()
        if heartbeat:
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    elapsed = int(time.time() - started)
    await request_snapshot(bot, delay=2)
    final_text = (
        f"✅ <b>MAIN_DATA cleanup complete</b>\n"
        f"Checked: {scanned}\nDeleted duplicates: <b>{deleted}</b>\n"
        f"Empty/unavailable: {skipped}\nElapsed: <b>{elapsed}s</b>\n\n"
        "Search index aur restart snapshot update ho raha hai."
    )
    await note.edit_text(final_text)
    if LOG_CHANNEL:
        try:
            await bot.send_message(LOG_CHANNEL, final_text)
        except Exception as exc:
            logging.warning("Cleanup final log failed: %s", exc)


@router.message(Command("diag"))
async def diag(message: Message, bot: Bot):
    if not await is_owner(message.from_user.id):
        return
    ch = await get_movie_channel()
    lines = [f"🎞 Movie channel: <code>{ch or 'NOT SET'}</code>",
             f"🗂 Log channel: <code>{LOG_CHANNEL or 'not set'}</code>",
             f"🎬 Indexed movies in storage: <b>{await db.movie_count(storage_only=True)}</b>",
             f"👥 Registered users: <b>{await db.user_count()}</b>"]
    if ch:
        try:
            me = await bot.get_chat_member(ch, (await bot.me()).id)
            lines.append(f"🤖 Bot status in channel: <b>{me.status}</b>")
        except Exception as e:
            lines.append(f"⚠️ Channel access error: <code>{e}</code>")
    try:
        main_chat = await bot.get_chat(MAIN_DATA)
        lines.append(f"📦 MAIN_DATA: <b>{main_chat.title or 'OK'}</b> <code>{MAIN_DATA}</code>")
        me_main = await bot.get_chat_member(MAIN_DATA, (await bot.me()).id)
        lines.append(f"🤖 Bot status in MAIN_DATA: <b>{me_main.status}</b>")
    except Exception as e:
        lines.append(f"⚠️ MAIN_DATA access error: <code>{e}</code>")
    if LOG_CHANNEL:
        try:
            m = await bot.send_message(LOG_CHANNEL, "diag ✅")
            await bot.delete_message(LOG_CHANNEL, m.message_id)
            lines.append("📨 Log channel write: OK")
        except Exception as e:
            lines.append(f"⚠️ Log channel error: <code>{e}</code>")
    try:
        from config import TG_API_ID, TG_API_HASH, TG_SESSION
        if TG_API_ID and TG_API_HASH and TG_SESSION:
            lines.append(f"🔑 Telethon: configured (API_ID={TG_API_ID})")
        else:
            lines.append("⚠️ Telethon: NOT configured — /scanall and /scanmain need TG_API_ID, TG_API_HASH, TG_SESSION")
    except Exception as e:
        lines.append(f"⚠️ Telethon config check: <code>{e}</code>")
    pool = helper_pool.get_stats()
    lines.append(f"🤖 Helper bots: {pool['active']}/{pool['total']} active")
    await message.answer("\n".join(lines), reply_markup=owner_panel_kb())


@router.callback_query(F.data == "op:scan")
async def op_scan(cb: CallbackQuery, state: FSMContext, bot: Bot):
    info = await resolve_channel(bot)
    if not info:
        await cb.answer()
        return await cb.message.answer("❌ Pehle 🎞 Movie Channel set karo.", reply_markup=owner_panel_kb())
    warn = ""
    if info.get("protected"):
        warn = ("\n\n🔒 <b>Warning:</b> is channel me <b>Restrict saving content</b> ON hai. "
                "Purani post scan karne ke liye use thodi der OFF karo, scan ke baad wapas ON kar do. "
                "Naye post fir bhi auto-index hote rahenge.")
    await _ask(cb, state, Own.scan,
               f"📡 <b>Scan movie channel</b>\n"
               f"Channel: <b>{info['title']}</b> "
               f"({'@' + info['username'] if info['username'] else 'private'}) "
               f"<code>{info['id']}</code>\n\n"
               "Scan kaise chalana hai:\n"
               "• <code>/scanfull</code> ya <code>/scanall</code> — poore configured channel ki complete history newest se oldest scan.\n"
               "• <code>/scan</code> — last/custom saved range scan.\n"
               "• <code>10000 1</code> — message ID 10000 se 1 tak custom range.\n"
               "• <code>5000 4000</code> — sirf message IDs 5000–4000 scan.\n"
               "• Channel ki koi bhi post <b>forward</b> karoge to bhi full history scan hogi; forwarded post ki ID range limit nahi banegi.\n\n"
               "Scan MAIN_DATA me unique media ka searchable index banata hai. Existing duplicate files dobara copy nahi hoti. Users ko movie isi MAIN_DATA channel ke original post se direct milti hai.\n\n"
               "⚠️ Full scan me IDs ke beech deleted/non-media posts bhi ho sakti hain, isliye progress me empty count normal hai."
               + warn)


@router.message(Command("scanall"))
@router.message(Command("scanfull"))
async def scanfull_command(message: Message, bot: Bot, state: FSMContext):
    if not await is_owner(message.from_user.id):
        await message.answer("❌ Sirf owner `/scanall` ya `/scanfull` chala sakta hai.")
        return
    await log_command(bot, message.from_user, "scanall")
    await state.set_state(Own.scan)
    arg = (message.text or "").partition(" ")[2].strip().lower()
    # "fresh" or "restart" clears resume cursors so the scan starts from scratch.
    if arg in {"fresh", "restart"}:
        from config import MAIN_CHANNELS
        channels = MAIN_CHANNELS if MAIN_CHANNELS else []
        for ch in channels:
            await db.set(f"scanall_cursor:{ch}", "0")
        await message.answer("🔄 Resume cursors cleared. Full scan shuru se start hoga.")
    scan_args = (message.text or "").partition(" ")[2].strip() or f"{SCAN_START_ID} 1"
    await do_scan(message, bot, state, scan_args=scan_args, enumerate_history=True)


@router.message(Command("scanmain"))
async def scanmain_command(message: Message, bot: Bot):
    """Fast MAIN_DATA-only scan with automatic retry and resume."""
    if not await is_owner(message.from_user.id):
        return await message.answer("❌ Sirf owner `/scanmain` chala sakta hai.")
    await log_command(bot, message.from_user, "scanmain")
    from config import TG_API_HASH, TG_API_ID, TG_SESSION
    if not (TG_API_ID and TG_API_HASH and TG_SESSION):
        return await message.answer(
            "❌ `/scanmain` ke liye TG_API_ID, TG_API_HASH aur TG_SESSION required hain.",
            reply_markup=owner_panel_kb(),
        )
    if _scanmain_lock.locked():
        return await message.answer("⏳ Main channel scan already chal raha hai.")

    async with _scanmain_lock:
        arg = (message.text or "").partition(" ")[2].strip().lower()
        resume_key = "main_import_cursor"
        if arg in {"fresh", "full", "restart"}:
            await db.set(resume_key, "0")
        note = await message.answer("⚡ <b>Main channel scan started</b>\nMAIN_DATA history read ho rahi hai…")
        started = time.time()
        last_edit = 0.0

        async def progress(st) -> None:
            nonlocal last_edit
            if time.time() - last_edit < 4:
                return
            last_edit = time.time()
            try:
                await note.edit_text(
                    f"⚡ <b>Main channel scanning…</b>\n"
                    f"🔎 Checked: <b>{st.checked}</b> | 🎬 Indexed: <b>{st.indexed}</b>\n"
                    f"⏭ Skipped: {st.skipped} | ⚠️ Errors: {st.errors}"
                )
            except Exception:
                pass

        from mtproto_importer import import_main_data
        try:
            stats = await import_main_data(progress=progress, resume_key=resume_key, attempts=5)
        except Exception as exc:
            logging.exception("/scanmain failed")
            return await message.answer(
                f"❌ Main channel scan fail hua: <code>{str(exc)[:300]}</code>\n"
                "Dobara <code>/scanmain</code> bhejo — jahan ruka tha wahin se resume hoga.",
                reply_markup=owner_panel_kb(),
            )

        # Persist immediately: Heroku ka filesystem restart par wipe ho jata hai.
        from mongo_store import save_movie_catalog, save_non_movie_state
        catalog_saved, state_saved = await asyncio.gather(save_movie_catalog(), save_non_movie_state())
        if not catalog_saved or not state_saved:
            logging.warning("scanmain persistence incomplete: catalog=%s state=%s", catalog_saved, state_saved)
        await db.set("catalog_migration_v1", "done")

        tail = ""
        if stats.last_error:
            tail = (f"\n⚠️ Last error: <code>{stats.last_error[:200]}</code>\n"
                    "Adhura raha to dobara <code>/scanmain</code> bhejo, resume ho jayega.")
        await message.answer(
            f"✅ <b>Main channel scan complete</b> ({int(time.time() - started)}s)\n"
            f"🔎 Checked: <b>{stats.checked}</b> | 🎬 Indexed: <b>{stats.indexed}</b>\n"
            f"⏭ Skipped: {stats.skipped} | ⚠️ Errors: {stats.errors}\n"
            f"📚 Total catalog: <b>{await db.movie_count()}</b>" + tail,
            reply_markup=owner_panel_kb(),
        )


@router.message(Command("scan"))
async def scan_command(message: Message, bot: Bot, state: FSMContext):
    if not await is_owner(message.from_user.id):
        return
    await log_command(bot, message.from_user, "scan", (message.text or "").partition(" ")[2])
    args = (message.text or "").partition(" ")[2].strip()
    if not args or args.lower() == "full":
        args = f"{SCAN_START_ID} 1"
    await state.set_state(Own.scan)
    await do_scan(message, bot, state, scan_args=args)


@router.message(Own.scan, ~F.text.startswith("/"))
async def do_scan(message: Message, bot: Bot, state: FSMContext, force_main_data: bool = False, scan_args: str = "", enumerate_history: bool = False):
    start_id, end_id = 0, 0
    if message.forward_from_message_id:
        # A forwarded post is only a trigger. Do not use its ID as the scan ceiling;
        # that made forwarding message 88 scan only 88→1.
        start_id = SCAN_START_ID
        end_id = 1
    else:
        parts = (scan_args or message.text or "").split()
        try:
            if len(parts) == 1:
                saved_start = await db.get("scan_last_start", "")
                saved_end = await db.get("scan_last_end", "")
                if saved_start and saved_end:
                    start_id, end_id = int(saved_start), int(saved_end)
                else:
                    start_id, end_id = int(parts[0]), 1
            else:
                start_id, end_id = int(parts[0]), int(parts[1])
        except Exception:
            return await message.answer(
            "Format: <code>start_id end_id</code> ya <code>full</code>.\n"
            "Example: <code>10000 1</code> = complete range ka sample.\n"
            "Kisi channel post ko forward karoge to full history scan start hogi."
        )
    if start_id <= 0 or end_id <= 0 or start_id < end_id:
        return await message.answer(
            "Newest message ID pehle aur oldest ID baad me do.\n"
            "Examples: <code>10000 1</code> ya <code>5000 4000</code>."
        )
    scan_start, scan_end = start_id, end_id

    await db.set("scan_last_start", str(scan_start))
    await db.set("scan_last_end", str(scan_end))
    # --- Instant start: validate everything in parallel, send started msg ASAP ---
    await state.clear()
    started = time.time()

    if enumerate_history:
        try:
            from config import TG_API_HASH, TG_API_ID, TG_SESSION
            if not (TG_API_ID and TG_API_HASH and TG_SESSION):
                return await message.answer(
                    "❌ `/scanall` ke liye TG_API_ID, TG_API_HASH aur TG_SESSION required hain.\n"
                    "Heroku config me Telethon StringSession set karo. Explicit range ke liye `/scan start end` use kar sakte ho.",
                    reply_markup=owner_panel_kb(),
                )
        except Exception as exc:
            return await message.answer(
                f"❌ Config error: <code>{str(exc)[:320]}</code>",
                reply_markup=owner_panel_kb(),
            )

    # Send the "started" message immediately so user sees instant feedback
    note = await message.answer(
        f"⚡ <b>Fast scan started</b>\n"
        "Telethon se newest-first direct read + batch index…"
    )

    # Parallel validation: check MAIN_DATA + all source channels at once
    if force_main_data:
        info = {"id": MAIN_DATA, "title": "MAIN_DATA", "username": ""}
        src = MAIN_DATA
        channel_ids = [MAIN_DATA]
    else:
        info = await resolve_channel(bot)
        src = info["id"] if info else await get_movie_channel()
        channel_raw = (await db.get("movie_channels", "")).strip()
        if not channel_raw:
            channel_raw = str(src or "")
        channel_ids = []
        for raw_id in channel_raw.split(","):
            value = raw_id.strip()
            if not value:
                continue
            channel_ids.append(int(value) if value.lstrip("-").isdigit() else (value if value.startswith("@") else f"@{value}"))
        if not channel_ids:
            return await message.answer(
                "❌ Source channel IDs set nahi hain. Owner Panel → Source Channels me "
                "comma-separated chat IDs bhejo, example: <code>-1001234567890,-1009876543210</code>.",
                reply_markup=owner_panel_kb(),
            )

    # Validate all channels in parallel (MAIN_DATA + all source channels)
    async def _check_chat(cid):
        try:
            return await bot.get_chat(cid)
        except Exception:
            return None
    dump = MAIN_DATA
    all_chats = await asyncio.gather(_check_chat(dump), *[_check_chat(cid) for cid in channel_ids])
    main_chat = all_chats[0]
    if main_chat is None:
        return await message.answer(
            f"❌ MAIN_DATA channel open nahi hua. Bot ko MAIN_DATA me administrator banao.",
            reply_markup=owner_panel_kb(),
        )
    for idx, chat in enumerate(all_chats[1:], 0):
        if chat is None:
            return await message.answer(
                f"❌ Source channel <code>{channel_ids[idx]}</code> open nahi hua.\n"
                "Chat ID sahi hai aur bot source channel me member/admin hai, ye check karo.",
                reply_markup=owner_panel_kb(),
            )

    resume_info = ""
    if enumerate_history:
        first_ch = channel_ids[0] if channel_ids else 0
        cursor_val = await db.get(f"scanall_cursor:{first_ch}", "0")
        if cursor_val and cursor_val.isdigit() and int(cursor_val) > 0:
            resume_info = f"\n🔄 Resuming from message #{cursor_val}"
    if resume_info:
        try:
            await note.edit_text(
                f"⚡ <b>Fast scan started</b>\n"
                f"Channel(s): <b>{len(channel_ids)}</b>\n"
                "Telethon se newest-first direct read + batch index…"
                + resume_info
            )
        except Exception:
            pass

    async def progress(done: int, total: int, st):
        try:
            await note.edit_text(
                f"⚡ <b>Fast scanning…</b>\n"
                f"🔎 Checked: <b>{done}</b> | 🎬 Indexed: <b>{st.found}</b>\n"
                f"⏭ Skipped: {st.skipped} | 🕳 empty: {st.missing} | ⚠️ errors: {st.errors}"
            )
        except Exception:
            pass

    aggregate = ScanStats()
    for channel_id in channel_ids:
        if enumerate_history:
            resume_key = f"scanall_cursor:{channel_id}"
            stats = await scan_channel_fast(bot, channel_id, dump, progress=progress, resume_key=resume_key)
        else:
            stats = await scan_range(bot, channel_id, dump, scan_start, scan_end, progress=progress, concurrency=32)
        for field in ("found", "skipped", "missing", "errors", "protected"):
            setattr(aggregate, field, getattr(aggregate, field) + getattr(stats, field))
        aggregate.last_error = stats.last_error or aggregate.last_error
        aggregate.samples.extend(stats.samples[: max(0, 3 - len(aggregate.samples))])
        if stats.stop_reason:
            aggregate.stop_reason = stats.stop_reason
            break
    stats = aggregate
    took = int(time.time() - started)

    if stats.stop_reason == "protected":
        return await message.answer(
            "🔒 <b>Scan rok diya.</b> Channel me <b>Restrict saving content</b> ON hai, "
            "isliye purani post padhi nahi ja sakti "
            f"(<code>{stats.last_error}</code>).\n\n"
            "Fix:\n1️⃣ Channel → Edit → <b>Restrict saving content</b> OFF karo\n"
            "2️⃣ Yahan wapas scan chalao\n"
            "3️⃣ Scan ke baad chaho to wapas ON kar do\n\n"
            "Ya kuch na karo — naye post auto-index hote rahenge aur storage catalog me add honge.",
            reply_markup=owner_panel_kb())
    if stats.stop_reason:
        return await message.answer(
            f"❌ Scan ruk gaya: <code>{stats.stop_reason}</code>\n\n"
            "Check karo bot movie channel + log channel dono me admin hai (/diag).",
            reply_markup=owner_panel_kb())

    # Heroku's filesystem is ephemeral. Persist each completed scan now rather
    # than waiting for the periodic task or a best-effort SIGTERM shutdown.
    from mongo_store import save_movie_catalog, save_non_movie_state
    catalog_saved, state_saved = await asyncio.gather(
        save_movie_catalog(),
        save_non_movie_state(),
    )
    if not catalog_saved or not state_saved:
        logging.warning("Completed scan persistence incomplete: catalog=%s state=%s", catalog_saved, state_saved)

    extra = ("\n\n" + "\n".join(stats.samples)) if stats.samples else ""
    resume_hint = ""
    if stats.errors > 0 and enumerate_history:
        resume_hint = "\n\n💡 Kuch errors aye. Dobara <code>/scanall</code> bhejo — jahan ruka tha wahin se resume hoga."
    await message.answer(
        f"✅ <b>Scan complete</b> ({took}s)\nSaved range: <code>{scan_start} {scan_end}</code>\n{stats.as_text()}\n"
        f"📦 Storage catalog: <b>{await db.movie_count(storage_only=True)}</b>{extra}{resume_hint}",
        reply_markup=owner_panel_kb())
    if LOG_CHANNEL:
        try:
            await bot.send_message(
                LOG_CHANNEL,
                f"📡 <b>Scan report</b>\nBy: {user_tag(message.from_user)}\n"
                f"Channel: {info['title'] if info else src} <code>{src}</code>\n"
                f"Range: {scan_start}→{scan_end} | Time: {took}s\n{stats.as_text()}",
            )
        except Exception as exc:
            logging.warning("Scan report log failed: %s", exc)


@router.message(Command("transferstatus"))
async def transfer_status(message: Message, bot: Bot):
    """Show multi-bot transfer system status"""
    if not await is_owner(message.from_user.id):
        return
    await log_command(bot, message.from_user, "transferstatus")
    pool = helper_pool.get_stats()
    tm = transfer_manager.stats
    text = (
        "🤖 <b>Multi-Bot Transfer Status</b>\n\n"
        f"📊 <b>Pool:</b> {pool['active']}/{pool['total']} bots active\n"
        f"🔄 <b>Active:</b> {tm['active']} transfers\n"
        f"✅ <b>Done:</b> {tm['done']}\n"
        f"❌ <b>Failed:</b> {tm['failed']}\n"
        f"⚡ <b>Speed:</b> {tm['speed']} msgs/min\n"
        f"⏱️ <b>Elapsed:</b> {tm['elapsed']}s\n\n"
        "<b>Bot Details:</b>\n"
    )
    for b in pool["bots"]:
        s = "🟢" if b["active"] else "🔴"
        text += f"{s} @{b['username']} | ✅{b['done']} ❌{b['failed']} | {b['rate']}\n"
    await message.answer(text)


@router.message(Command("reactivatebots"))
async def reactivate_bots_cmd(message: Message, bot: Bot):
    """Reactivate deactivated helper bots"""
    if not await is_owner(message.from_user.id):
        return
    await log_command(bot, message.from_user, "reactivatebots")
    await helper_pool.reactivate_bots()
    active = helper_pool.active_count
    await message.answer(f"✅ Reactivated. Active: {active}/{helper_pool.total_bots}")


@router.message(Command("setsupport"))
async def set_support_url(message: Message, bot: Bot):
    """Set support URL for 'movie not found' button."""
    if not await is_owner(message.from_user.id):
        return
    await log_command(bot, message.from_user, "setsupport")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        current = await db.get("support_url", "")
        await message.answer(
            f"📞 <b>Support URL Setting</b>\n\n"
            f"Current URL: <code>{current or 'Not set'}</code>\n\n"
            "Usage: <code>/setsupport https://t.me/your_group</code>\n"
            "Ya koi bhi URL daalo jahan users movie request kar sakein.\n\n"
            "Disable karne ke liye: <code>/setsupport off</code>"
        )
        return
    url = parts[1].strip()
    if url.lower() in ("off", "none", "disable", ""):
        await db.set("support_url", "")
        await message.answer("✅ Support button disabled. Ab 'not found' par button nahi dikhega.")
    else:
        await db.set("support_url", url)
        await message.answer(f"✅ Support URL set ho gaya:\n<code>{url}</code>\n\nAb jab movie nahi milegi, users ko ye button dikhega.")


@router.message(Command("ban"))
async def ban_user_cmd(message: Message, bot: Bot):
    """Ban a user from using the bot."""
    if not await is_owner(message.from_user.id):
        return
    await log_command(bot, message.from_user, "ban")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("Usage: <code>/ban user_id</code>")
    try:
        uid = int(parts[1].strip())
    except ValueError:
        return await message.answer("Valid numeric user ID bhejo.")
    if uid in OWNER_IDS:
        return await message.answer("❌ Owner ko ban nahi kar sakte.")
    banned = await db.ban_user(uid)
    if banned:
        await log_owner_action(bot, message.from_user.id, "ban_user", f"User {uid} banned")
        await message.answer(f"✅ User <code>{uid}</code> ko ban kar diya gaya.")
    else:
        await message.answer(f"❌ User <code>{uid}</code> database me nahi mila.")


@router.message(Command("unban"))
async def unban_user_cmd(message: Message, bot: Bot):
    """Unban a previously banned user."""
    if not await is_owner(message.from_user.id):
        return
    await log_command(bot, message.from_user, "unban")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("Usage: <code>/unban user_id</code>")
    try:
        uid = int(parts[1].strip())
    except ValueError:
        return await message.answer("Valid numeric user ID bhejo.")
    unbanned = await db.unban_user(uid)
    if unbanned:
        await log_owner_action(bot, message.from_user.id, "unban_user", f"User {uid} unbanned")
        await message.answer(f"✅ User <code>{uid}</code> ka ban hata diya gaya.")
    else:
        await message.answer(f"❌ User <code>{uid}</code> database me nahi mila.")


@router.message(Command("userinfo"))
async def user_info_cmd(message: Message, bot: Bot):
    """Show details about a specific user."""
    if not await is_owner(message.from_user.id):
        return
    await log_command(bot, message.from_user, "userinfo")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("Usage: <code>/userinfo user_id</code>")
    try:
        uid = int(parts[1].strip())
    except ValueError:
        return await message.answer("Valid numeric user ID bhejo.")
    user = await db.user(uid)
    if not user:
        return await message.answer(f"❌ User <code>{uid}</code> nahi mila.")
    status = "BANNED" if user["banned"] else ("Premium" if is_premium(user) else "Free")
    history = await db.history(uid)
    text = (
        f"👤 <b>User Info</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"👤 Name: {user['name'] or '-'}\n"
        f"💬 Username: @{user['username'] or '-'}\n"
        f"📋 Status: <b>{status}</b>\n"
        f"👥 Referrals: <b>{user['referrals']}</b>\n"
        f"🎬 Movies watched: <b>{len(history)}</b>\n"
    )
    if is_premium(user):
        text += f"💎 Premium left: {human_left(user['premium_until'])}\n"
    await message.answer(text, reply_markup=owner_panel_kb())


class _JoinVisitFlow(StatesGroup):
    waiting_join = State()
    waiting_visit = State()


@router.callback_query(F.data == "op:setjoin")
async def op_setjoin(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in OWNER_IDS:
        await cb.answer("Only owner", show_alert=True)
        return
    import os as _os
    cur = await db.get("join_channel", "") or _os.getenv("JOIN_CHANNEL", "") or "-"
    await state.set_state(_JoinVisitFlow.waiting_join)
    await cb.message.answer(f"📢 Current Join: <code>{cur}</code>\n\nNaya username/URL bhejo:")
    await cb.answer()


@router.message(_JoinVisitFlow.waiting_join)
async def op_setjoin_val(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    await db.set("join_channel", val)
    await state.clear()
    logging.info("🔧 OWNER changed JOIN -> %s", val)
    await message.answer(f"✅ Join set: <code>{val}</code>", reply_markup=owner_panel_kb())


@router.callback_query(F.data == "op:setvisit")
async def op_setvisit(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in OWNER_IDS:
        await cb.answer("Only owner", show_alert=True)
        return
    import os as _os
    cur = await db.get("visit_channel", "") or _os.getenv("VISIT_CHANNEL", "") or "-"
    await state.set_state(_JoinVisitFlow.waiting_visit)
    await cb.message.answer(f"🌐 Current Visit: <code>{cur}</code>\n\nNaya username/URL bhejo:")
    await cb.answer()


@router.message(_JoinVisitFlow.waiting_visit)
async def op_setvisit_val(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    await db.set("visit_channel", val)
    await state.clear()
    logging.info("🔧 OWNER changed VISIT -> %s", val)
    await message.answer(f"✅ Visit set: <code>{val}</code>", reply_markup=owner_panel_kb())


# ---------------- Helper Bot Controls ----------------

@router.callback_query(F.data == "op:helpers")
async def op_helpers(cb: CallbackQuery, bot: Bot):
    """Show helper bot pool status and controls."""
    if not await is_owner(cb.from_user.id):
        return await cb.answer("Only owner", show_alert=True)
    await cb.answer()
    pool = helper_pool.get_stats()
    tm = transfer_manager.stats
    text = (
        "🤖 <b>Helper Bot Pool</b>\n\n"
        f"📊 Active: <b>{pool['active']}/{pool['total']}</b> bots\n"
        f"🔄 Active transfers: {tm['active']}\n"
        f"✅ Done: {tm['done']}\n"
        f"❌ Failed: {tm['failed']}\n\n"
        "<b>Bot Details:</b>\n"
    )
    for b in pool["bots"]:
        status = "🟢" if b["active"] else "🔴"
        text += f"{status} @{b['username']} | ✅{b['done']} ❌{b['failed']} | {b['rate']}\n"
        if b["error"]:
            text += f"   └ <code>{b['error']}</code>\n"
    if not pool["bots"]:
        text += "\n⚠️ <b>Koi helper bot configure nahi hai.</b>\n\nHelper bots add karne ke liye .env me <code>HELPER_BOT_TOKENS</code> set karo:\n<code>HELPER_BOT_TOKENS=123:AAA,456:BBB,789:CCC</code>\n\nHar helper bot ko source channels me admin banakar add karo. Wo automatically naye movies ko MAIN_DATA me transfer karenge."

    from aiogram.types import InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [button("🔄 Reactivate All Bots", callback_data="op:reactivate")],
        [button("📊 Refresh Stats", callback_data="op:helpers")],
        [button("⬅️ Owner Panel", callback_data="op:home")],
    ])
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "op:reactivate")
async def op_reactivate(cb: CallbackQuery, bot: Bot):
    if not await is_owner(cb.from_user.id):
        return await cb.answer("Only owner", show_alert=True)
    await cb.answer("Reactivating...")
    await helper_pool.reactivate_bots()
    active = helper_pool.active_count
    await cb.message.answer(
        f"✅ Bots reactivated: <b>{active}/{helper_pool.total_bots}</b> active",
        reply_markup=back_panel()
    )


@router.callback_query(F.data == "op:autotransfer")
async def op_autotransfer(cb: CallbackQuery, bot: Bot):
    """Toggle instant auto-transfer from source channels."""
    if not await is_owner(cb.from_user.id):
        return await cb.answer("Only owner", show_alert=True)
    await cb.answer()
    current = await db.get("auto_transfer_enabled", "1")
    status = "🟢 ON" if current == "1" else "🔴 OFF"
    text = (
        f"⚡ <b>Auto-Transfer Mode</b>\n\n"
        f"Current: <b>{status}</b>\n\n"
        "<b>ON</b> (default): Jab bhi kisi source channel me naya movie aata hai, "
        "helper bots turant usko MAIN_DATA me transfer kar dete hain. "
        "Forward fail ho to copy, copy fail ho to MTProto fallback.\n\n"
        "<b>OFF</b>: Har naye movie ke liye owner ko manual approval request aati hai."
    )
    from aiogram.types import InlineKeyboardMarkup
    new_val = "0" if current == "1" else "1"
    new_label = "🔴 Disable Auto-Transfer" if current == "1" else "🟢 Enable Auto-Transfer"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [button(new_label, callback_data=f"op:autotransfer_set:{new_val}")],
        [button("⬅️ Owner Panel", callback_data="op:home")],
    ])
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("op:autotransfer_set:"))
async def op_autotransfer_set(cb: CallbackQuery):
    if not await is_owner(cb.from_user.id):
        return await cb.answer("Only owner", show_alert=True)
    val = cb.data.split(":")[2]
    await db.set("auto_transfer_enabled", val)
    await cb.answer()
    status = "🟢 ENABLED" if val == "1" else "🔴 DISABLED"
    await cb.message.answer(
        f"✅ Auto-transfer {status}.\n"
        + ("Naye source movies ab instant MAIN_DATA me transfer honge." if val == "1"
           else "Ab har naye movie ke liye manual approval aayega."),
        reply_markup=owner_panel_kb()
    )


@router.callback_query(F.data == "op:groupmode")
async def op_groupmode(cb: CallbackQuery):
    """Toggle group search mode on/off."""
    if not await is_owner(cb.from_user.id):
        return await cb.answer("Only owner", show_alert=True)
    await cb.answer()
    current = await db.get("group_mode", "1")
    status = "🟢 ON" if current == "1" else "🔴 OFF"
    text = (
        f"👥 <b>Group Search Mode</b>\n\n"
        f"Current: <b>{status}</b>\n\n"
        "<b>ON</b> (default): Bot groups me respond karta hai jab user bot ko "
        "@mention kare ya bot ke message reply kare. Movie search group me chalegi.\n\n"
        "<b>OFF</b>: Bot groups me koi bhi search ya message ka jawab nahi dega. "
        "Sirf DM me search chalegi."
    )
    from aiogram.types import InlineKeyboardMarkup
    new_val = "0" if current == "1" else "1"
    new_label = "🔴 Disable Group Search" if current == "1" else "🟢 Enable Group Search"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [button(new_label, callback_data=f"op:groupmode_set:{new_val}", style="success" if current == "1" else "danger")],
        [button("⬅️ Owner Panel", callback_data="op:home")],
    ])
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("op:groupmode_set:"))
async def op_groupmode_set(cb: CallbackQuery):
    if not await is_owner(cb.from_user.id):
        return await cb.answer("Only owner", show_alert=True)
    val = cb.data.split(":")[2]
    await db.set("group_mode", val)
    await cb.answer()
    status = "🟢 ENABLED" if val == "1" else "🔴 DISABLED"
    await cb.message.answer(
        f"✅ Group search mode {status}.\n"
        + ("Ab bot groups me @mention ya reply par search karega." if val == "1"
           else "Ab bot groups me koi search ka jawab nahi dega. Sirf DM me search chalegi."),
        reply_markup=owner_panel_kb()
    )
