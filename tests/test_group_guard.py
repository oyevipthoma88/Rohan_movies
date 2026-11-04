import os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import db as db_module
from db import DB
from group_guard import group_command, ALLOWED_GROUP_COMMANDS


class GroupGuardTests(unittest.TestCase):
    def test_plain_text_is_not_a_command(self):
        self.assertIsNone(group_command("toxic movie bhejo", "RohanBot"))

    def test_movie_command_detected_with_and_without_mention(self):
        self.assertEqual(group_command("/movie Toxic", "RohanBot"), "movie")
        self.assertEqual(group_command("/movie@RohanBot Toxic", "RohanBot"), "movie")
        self.assertIn("movie", ALLOWED_GROUP_COMMANDS)

    def test_command_for_other_bot_ignored(self):
        self.assertIsNone(group_command("/movie@OtherBot Toxic", "RohanBot"))


class GroupMemberTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_module.DB_PATH = os.path.join(self.tmp.name, "bot.db")
        self.db = DB(); await self.db.init()

    async def asyncTearDown(self):
        await self.db.close(); self.tmp.cleanup()

    async def test_only_recent_members_are_taggable_and_kicked_group_skipped(self):
        await self.db.remember_group_member(-100, "G", 1, "A", "a")
        await self.db.conn.execute("UPDATE group_members SET last_seen=0 WHERE user_id=1")
        await self.db.remember_group_member(-100, "G", 2, "B", "b")
        ids = [r["user_id"] for r in await self.db.recent_group_members(-100, int(time.time()) - 7 * 86400)]
        self.assertEqual(ids, [2])
        await self.db.set_group_active(-100, False)
        self.assertEqual(await self.db.active_group_ids(), [])
