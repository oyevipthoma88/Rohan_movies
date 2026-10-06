import sys
sys.path.insert(0, "src")

from keyboards import approve_kb, force_join_kb, main_menu, owner_panel_kb, paid_kb, plans_kb, results_kb

markups = [
    force_join_kb([("Movies", "https://t.me/example")]),
    main_menu(True),
    plans_kb([{"label": "7 Days", "price": 149, "code": "7day"}]),
    paid_kb("7day"),
    approve_kb(1, 2, "7day"),
    owner_panel_kb(),
    results_kb([{"title": "Test", "year": "2026", "quality": "HD", "size": "1 GB", "msg_id": 1}], "test"),
]

buttons = [button for markup in markups for row in markup.inline_keyboard for button in row]
assert all(button.text != "Support" for button in buttons)
assert all(button.style for button in buttons), [(button.text, button.style) for button in buttons if not button.style]
assert all(button.icon_custom_emoji_id for button in buttons), [button.text for button in buttons if not button.icon_custom_emoji_id]
print(f"checked {len(buttons)} buttons: style and custom emoji active; Support absent")
