import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import db as db_module
from movie_catalog import detect_content_type


class TopMoviesDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_path = db_module.DB_PATH
        db_module.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        self.database = db_module.DB()
        await self.database.init()

    async def asyncTearDown(self):
        await self.database.conn.close()
        db_module.DB_PATH = self.previous_path
        self.temp_dir.cleanup()

    async def test_popular_movies_uses_successful_delivery_count(self):
        await self.database.upsert_movie(101, "Most Requested", "2026", "1080p", "Hindi", "1GB", "", storage_msg_id=101)
        await self.database.upsert_movie(102, "Second Place", "2026", "720p", "Hindi", "900MB", "", storage_msg_id=102)

        for user_id in (1, 2, 3):
            await self.database.add_history(user_id, "most", "Most Requested", 101)
        await self.database.add_history(4, "second", "Second Place", 102)

        movies = await self.database.popular_movies(50)

        self.assertEqual([movie["msg_id"] for movie in movies], [101, 102])
        self.assertEqual([movie["request_count"] for movie in movies], [3, 1])
        self.assertLessEqual(len(movies), 15)

    async def test_recent_movies_limits_results_to_fifteen(self):
        for message_id in range(1, 18):
            await self.database.upsert_movie(message_id, f"Movie {message_id}", "2026", "720p", "Hindi", "", "")

        movies = await self.database.recent_movies(limit=99)

        self.assertEqual(len(movies), 15)
        self.assertTrue(all(movie["request_count"] == 0 for movie in movies))

    async def test_catalog_type_is_stored_and_filterable(self):
        await self.database.upsert_movie(
            201, "Example Anime", "2026", "1080p", "Japanese", "", "New Anime Episode", content_type="anime"
        )
        await self.database.upsert_movie(
            202, "Example Series", "2026", "1080p", "Hindi", "", "Web Series Season 1", content_type="series"
        )

        anime = await self.database.recent_movies("anime")
        series = await self.database.recent_movies("series")

        self.assertEqual([movie["msg_id"] for movie in anime], [201])
        self.assertEqual([movie["msg_id"] for movie in series], [202])
        self.assertEqual(detect_content_type("Anime Episode 2"), "anime")
        self.assertEqual(detect_content_type("Web Series S01E02"), "series")
        self.assertEqual(detect_content_type("Hindi Movie 2026"), "movie")

    async def test_popular_movies_is_capped_and_excludes_non_storage_rows(self):
        for message_id in range(301, 306):
            await self.database.upsert_movie(
                message_id, f"Movie {message_id}", "2026", "1080p", "Hindi", "", "",
                storage_msg_id=message_id,
            )
            await self.database.add_history(1, "top", f"Movie {message_id}", message_id)
        await self.database.upsert_movie(999, "Unsendable", "2026", "1080p", "Hindi", "", "")
        await self.database.add_history(2, "top", "Unsendable", 999)

        movies = await self.database.popular_movies(limit=5)

        self.assertEqual(len(movies), 5)
        self.assertTrue(all(movie["storage_msg_id"] > 0 for movie in movies))
        self.assertTrue(all(movie["request_count"] == 1 for movie in movies))
