import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import db as db_module
from db import DB
from keyboards import main_menu, owner_panel_kb, source_approval_kb
import utils
from utils import search_movies


class RequestedFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_module.DB_PATH = os.path.join(self.tempdir.name, "bot.db")
        self.db = DB()
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self.tempdir.cleanup()

    async def test_coupon_is_case_and_space_insensitive_and_single_use(self):
        await self.db.add_user(101, "u", "User")
        await self.db.add_coupon("ROHAN50", 7, 1)
        self.assertEqual(await self.db.redeem_coupon(" rohan 50 ", 101), 7)
        self.assertIsNone(await self.db.redeem_coupon("ROHAN50", 101))
        await self.db.add_user(102, "v", "Other")
        self.assertIsNone(await self.db.redeem_coupon("ROHAN50", 102))

    async def test_new_settings_and_expiry_are_snapshot_persisted(self):
        self.assertIn("Movie Bot Tutorial", await self.db.get("tutorial"))
        self.assertEqual(await self.db.get("online_player_bot"), "")
        self.assertEqual(await self.db.get("auto_delete_minutes"), "7")
        await self.db.schedule_expiry(101, 202, 9999999999)
        state = await self.db.export_state()
        self.assertEqual(len(state["tables"]["delivery_expiry"]), 1)

    async def test_typo_search_returns_close_title(self):
        await self.db.upsert_movie(1, "Interstellar", "2014", "1080p", "English", "1GB", "", "f1", "video", "movie", "", 10)
        await self.db.upsert_movie(2, "Mirzapur Season 1 Episode 1", "2018", "1080p", "Hindi", "1GB", "", "f2", "video", "movie", "", 11)
        await self.db.upsert_movie(3, "Mirzapur Season 2 Episode 1", "2020", "1080p", "Hindi", "1GB", "", "f3", "video", "movie", "", 12)
        original = utils.db
        utils.db = self.db
        try:
            results = await search_movies("interstelar", content_type="movie")
            mirzapur_results = await search_movies("Mirjapur", content_type="movie")
        finally:
            utils.db = original
        self.assertEqual(results[0]["title"], "Interstellar")
        self.assertEqual({row["title"] for row in mirzapur_results}, {"Mirzapur Season 1 Episode 1", "Mirzapur Season 2 Episode 1"})

    async def test_owner_button_removed_and_owner_controls_present(self):
        user_buttons = [b.text for row in main_menu(False).inline_keyboard for b in row]
        self.assertNotIn("👑 Owner", user_buttons)
        owner_buttons = [b.text for row in owner_panel_kb().inline_keyboard for b in row]
        self.assertIn("📘 Set Tutorial", owner_buttons)
        self.assertIn("🎥 Watch Online Bot", owner_buttons)
        self.assertIn("🔗 Verify URLs (max 5)", owner_buttons)
        approval_buttons = [b.callback_data for row in source_approval_kb(-100123, 77).inline_keyboard for b in row]
        self.assertIn("srcappr:-100123:77", approval_buttons)
        self.assertIn("srcdecl:-100123:77", approval_buttons)

    async def test_payment_approval_is_atomic_and_single_use(self):
        await self.db.add_user(303, "payer", "Payer")
        pid = await self.db.add_payment(303, "1day")
        first = await self.db.approve_payment(pid, 303, 1)
        second = await self.db.approve_payment(pid, 303, 1)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        user = await self.db.user(303)
        self.assertEqual(user["premium_until"], first)


if __name__ == "__main__":
    unittest.main()
