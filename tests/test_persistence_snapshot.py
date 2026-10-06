import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import db as db_module


class PersistenceSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_path = db_module.DB_PATH
        db_module.DB_PATH = str(Path(self.temp_dir.name) / "source.db")
        self.source = db_module.DB()
        await self.source.init()

    async def asyncTearDown(self):
        await self.source.close()
        db_module.DB_PATH = self.previous_path
        self.temp_dir.cleanup()

    async def test_state_round_trip_restores_catalog_and_settings(self):
        await self.source.add_user(123, "rohan", "Rohan")
        await self.source.set("caption_join", "@movies")
        await self.source.ensure_stats()
        await self.source.incr_stat("completed_scans")
        await self.source.upsert_movie(
            77, "Interstellar", "2014", "1080p", "Hindi", "2GB",
            "Interstellar 2014 1080p Hindi\n⭐ IMAX version",
            "file-id", "video", "movie", duration="2:49", storage_msg_id=77,
        )
        state = await self.source.export_state()

        second_path = str(Path(self.temp_dir.name) / "restored.db")
        db_module.DB_PATH = second_path
        restored = db_module.DB()
        await restored.init()
        await restored.import_state(state)
        movie = await restored.movie(77)
        user = await restored.user(123)
        self.assertEqual(movie["title"], "Interstellar")
        self.assertEqual(movie["duration"], "2:49")
        self.assertEqual(movie["storage_msg_id"], 77)
        self.assertEqual(user["username"], "rohan")
        self.assertEqual(await restored.get("caption_join"), "@movies")
        cur = await restored.conn.execute("SELECT count FROM stats WHERE key=?", ("completed_scans",))
        self.assertEqual((await cur.fetchone())["count"], 1)
        await restored.close()


if __name__ == "__main__":
    unittest.main()

