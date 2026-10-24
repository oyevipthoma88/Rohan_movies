from __future__ import annotations

import os

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


BUTTON_STYLE = os.getenv("BUTTON_STYLE", "1")
BUTTON_ICON_CUSTOM_EMOJI_ID = os.getenv("BUTTON_ICON_CUSTOM_EMOJI_ID", "5443038326535759644")
DEFAULT_BUTTON_STYLE = os.getenv("DEFAULT_BUTTON_STYLE", "primary")


def button(text: str, *, callback_data: str | None = None, url: str | None = None,
           icon_custom_emoji_id: str | None = BUTTON_ICON_CUSTOM_EMOJI_ID,
           style: str | None = None) -> InlineKeyboardButton:
    """Build a Telegram button with optional newer custom-emoji/style fields.

    Optional fields are added only when the installed Telegram/aiogram model
    exposes them; otherwise the button remains a standard compatible button.
    """
    kwargs = {"text": text, "callback_data": callback_data, "url": url}
    supported = getattr(InlineKeyboardButton, "model_fields", {})
    if icon_custom_emoji_id and "icon_custom_emoji_id" in supported:
        kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
    effective_style = style or DEFAULT_BUTTON_STYLE
    if effective_style and BUTTON_STYLE and "style" in supported:
        kwargs["style"] = effective_style
    return InlineKeyboardButton(**kwargs)


def force_join_kb(channels: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for title, link in channels:
        kb.row(button(f"📢 Join {title}", url=link))
    kb.row(button("✅ Verify", callback_data="verify", style="success"))
    return kb.as_markup()


def main_menu(is_owner: bool, is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(button("🎬 Movies", callback_data="get_movie", style="success"))
    kb.row(
        button("📺 Web Series", callback_data="get_series"),
        button("🎌 Anime", callback_data="get_anime"),
    )
    kb.row(
        button("🔥 Trending", callback_data="trending"),
        button("🆕 Recently Added", callback_data="recent"),
    )
    kb.row(
        button("📘 Tutorial", callback_data="tutorial"),
        button("🕘 History", callback_data="history"),
    )
    kb.row(
        button("💎 Premium", callback_data="plans", style="success"),
        button("🎟 Coupon", callback_data="coupon"),
    )
    kb.row(
        button("🎁 Refer & Earn", callback_data="refer"),
        button("🎥 Watch Online", callback_data="online_player"),
    )
    if is_owner:
        kb.row(button("🛠 Owner Panel", callback_data="op:home"))
    elif is_admin:
        kb.row(button("➕ Add Video", callback_data="admin:addvideo"))
    return kb.as_markup()


def results_kb(movies: list, query: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for m in movies:
        label = f"🎬 {m['title']}"
        if m["year"]:
            label += f" ({m['year']})"
        label += f" • {m['quality']}"
        if m["size"]:
            label += f" • {m['size']}"
        kb.row(button(label[:64], callback_data=f"send:{m['msg_id']}", style="success"))
    kb.row(button("🏠 Home", callback_data="home"))
    return kb.as_markup()


def _plan_field(plan, name: str, default: str = "") -> str:
    try:
        value = plan[name]
    except (KeyError, IndexError, TypeError):
        return default
    return str(value) if value not in (None, "") else default


def plan_label(plan) -> str:
    """Single source of truth for how a plan is shown to owner AND users."""
    emoji = _plan_field(plan, "emoji", "🎟️")
    text = f"{emoji} {plan['label']} — ₹{plan['price']}"
    tag = _plan_field(plan, "tag")
    if tag:
        text += f" — {tag}"
    return text


def plans_text(plans: list, title: str = "💰 <b>CURRENT PLANS</b>") -> str:
    """One message with every plan — exactly what the owner set."""
    lines = "\n\n".join(plan_label(p) for p in plans)
    return f"{title}\n\n{lines}"


def plans_kb(plans: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in plans:
        kb.row(button(plan_label(p)[:64], callback_data=f"buy:{p['code']}", style="success"))
    kb.row(button("🎟 Coupon Code", callback_data="coupon"))
    kb.row(button("🏠 Home", callback_data="home"))
    return kb.as_markup()


def paid_kb(code: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(button("📸 Payment Screenshot Bhejo", callback_data=f"ss:{code}"))
    kb.row(button("🏠 Home", callback_data="home"))
    return kb.as_markup()


def approve_kb(pid: int, user_id: int, code: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        button("✅ Approve", callback_data=f"appr:{pid}:{user_id}:{code}"),
        button("❌ Reject", callback_data=f"rej:{pid}:{user_id}", style="danger"),
    )
    return kb.as_markup()


def admin_panel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(button("➕ Add Video", callback_data="admin:addvideo"))
    kb.row(button("🏠 Home", callback_data="home"))
    return kb.as_markup()


def owner_panel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        button("🖼 Start Pic Set", callback_data="op:startpic"),
        button("💳 QR Set", callback_data="op:qr"),
    )
    kb.row(
        button("💰 Plan Price", callback_data="op:price"),
        button("🎁 Gift Premium", callback_data="op:gift"),
    )
    kb.row(
        button("🎟 Create Coupon", callback_data="op:coupon"),
        button("💎 Premium Users", callback_data="op:premlist"),
    )
    kb.row(
        button("🎞 Source Channels", callback_data="op:setchannel"),
        button("📡 Scan Movie Channel", callback_data="op:scan"),
        button("📢 Broadcast", callback_data="op:broadcast"),
    )
    kb.row(
        button("👮 Manage Admin", callback_data="op:admin"),
        button("🔗 Verify URLs (max 5)", callback_data="op:force"),
    )
    kb.row(
        button("📘 Set Tutorial", callback_data="op:tutorial"),
        button("🎥 Watch Online Bot", callback_data="op:online"),
    )
    kb.row(
        button("📝 Caption Style", callback_data="op:caption"),
        button("🗑 Auto Delete", callback_data="op:autodelete"),
    )
    kb.row(
        button("📢 Join Channel", callback_data="op:setjoin"),
        button("🌐 Visit Channel", callback_data="op:setvisit"),
    )
    kb.row(
        button("⚡ Auto-Transfer", callback_data="op:autotransfer"),
        button("👥 Group Mode", callback_data="op:groupmode"),
    )
    kb.row(
        button("📊 Stats", callback_data="op:stats"),
        button("🤖 Helper Bots", callback_data="op:helpers"),
    )
    kb.row(button("🏠 Home", callback_data="home"))
    return kb.as_markup()


def back_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[button("⬅️ Owner Panel", callback_data="op:home")]]
    )


def source_approval_kb(source_chat_id: int, source_message_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        button("✅ Add to MAIN_DATA", callback_data=f"srcappr:{source_chat_id}:{source_message_id}", style="success"),
        button("❌ Decline", callback_data=f"srcdecl:{source_chat_id}:{source_message_id}", style="danger"),
    )
    return kb.as_markup()


def movie_link_kb(link: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(button("⬇️ Movie Kholo / Download", url=link))
    kb.row(button("🏠 Home", callback_data="home"))
    return kb.as_markup()


def browse_kb(movies: list, page: int = 0, per_page: int = 10, callback_prefix: str = "browse") -> InlineKeyboardMarkup:
    """Paginated browse keyboard for trending/recent movie lists."""
    kb = InlineKeyboardBuilder()
    start = page * per_page
    end = min(start + per_page, len(movies))
    for m in movies[start:end]:
        label = f"🎬 {m['title']}"
        if m.get("year"):
            label += f" ({m['year']})"
        label += f" • {m.get('quality', '') or 'HD'}"
        if m.get("size"):
            label += f" • {m['size']}"
        kb.row(button(label[:64], callback_data=f"send:{m['msg_id']}", style="success"))
    nav = []
    if page > 0:
        nav.append(button("⬅️ Prev", callback_data=f"{callback_prefix}:{page - 1}"))
    if end < len(movies):
        nav.append(button("Next ➡️", callback_data=f"{callback_prefix}:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(button("🏠 Home", callback_data="home"))
    return kb.as_markup()
