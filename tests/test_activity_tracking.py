import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import db as db_module
from db import DB


class ActivityTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_module.DB_PATH = os.path.join(self.tmp.name, "bot.db")
        self.db = DB(); await self.db.init()

    async def asyncTearDown(self):
        await self.db.close(); self.tmp.cleanup()

    async def test_button_only_user_is_saved_with_name(self):
        self.assertTrue(await self.db.track_user(5, "rohan", "Rohan K", "hi", True, "button:menu"))
        row = await self.db.user(5)
        self.assertEqual((row["name"], row["username"], row["actions"]), ("Rohan K", "rohan", 1))
        self.assertFalse(await self.db.track_user(5, "rohan2", "Rohan", "", False, "text"))
        self.assertEqual((await self.db.user(5))["actions"], 2)

    async def test_start_referral_still_counts_after_tracking(self):
        await self.db.add_user(1, "ref", "Ref")
        self.assertTrue(await self.db.add_user(2, "n", "New", referred_by=1))
        await self.db.track_user(2, "n", "New", action="command:start")
        self.assertEqual((await self.db.user(1))["referrals"], 1)

    async def test_blocked_users_skipped_in_broadcast(self):
        await self.db.track_user(1, "a", "A"); await self.db.track_user(2, "b", "B")
        await self.db.set_blocked(2, True)
        self.assertEqual(await self.db.broadcast_user_ids(), [1])
        await self.db.track_user(2, "b", "B")  # user came back
        self.assertEqual(await self.db.broadcast_user_ids(), [1, 2])

    async def test_gifted_placeholder_name_gets_replaced(self):
        await self.db.add_user(9, "", "gifted")
        await self.db.add_user(9, "real", "Real Name")
        self.assertEqual((await self.db.user(9))["name"], "Real Name")
