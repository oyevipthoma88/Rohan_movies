from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
from typing import Any, Optional

from config import (DB_PATH, DEFAULT_PLANS, MONGO_CAPTION_LIMIT, PLAN_PRICING_REVISION,
                    DEFAULT_FORCE_CHANNELS, DEFAULT_START_PIC, DEFAULT_QR, DEFAULT_UPI,
                    DEFAULT_AUTO_DELETE_MINUTES)
from movie_catalog import detect_content_type

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    joined_at INTEGER,
    premium_until INTEGER DEFAULT 0,
    free_used_today INTEGER DEFAULT 0,
    last_free_day TEXT DEFAULT '',
    first_bonus_used INTEGER DEFAULT 0,
    referred_by INTEGER DEFAULT 0,
    referrals INTEGER DEFAULT 0,
    rewarded_upto INTEGER DEFAULT 0,
    banned INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS movies (
    msg_id INTEGER PRIMARY KEY,
    title TEXT,
    year TEXT,
    quality TEXT,
    language TEXT,
    size TEXT,
    duration TEXT DEFAULT '',
    caption TEXT,
    file_id TEXT,
    file_type TEXT,
    storage_msg_id INTEGER DEFAULT 0,
    content_type TEXT DEFAULT 'movie',
    added_at INTEGER
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    query TEXT,
    title TEXT,
    msg_id INTEGER,
    at INTEGER
);
CREATE TABLE IF NOT EXISTS plans (
    code TEXT PRIMARY KEY,
    label TEXT,
    price INTEGER,
    days INTEGER
);
CREATE TABLE IF NOT EXISTS coupons (
    code TEXT PRIMARY KEY,
    days INTEGER,
    max_uses INTEGER DEFAULT 1,
    used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS coupon_uses (
    code TEXT, user_id INTEGER, PRIMARY KEY (code, user_id)
);
CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    plan TEXT,
    status TEXT DEFAULT 'pending',
    at INTEGER
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    event TEXT,
    details TEXT,
    at INTEGER
);
CREATE TABLE IF NOT EXISTS delivery_expiry (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delete_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_history_msg_id_at ON history (msg_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_delivery_expiry_delete_at ON delivery_expiry (delete_at);
"""


class DB:
    conn: aiosqlite.Connection
    _USER_UPDATE_FIELDS = frozenset({
        "username", "name", "premium_until", "free_used_today", "last_free_day",
        "first_bonus_used", "referred_by", "referrals", "rewarded_upto", "banned",
    })

    async def init(self) -> None:
        Path(DB_PATH).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(DB_PATH)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA busy_timeout=10000")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        for code, label, price, days, emoji, tag, sort in DEFAULT_PLANS:
            await self.conn.execute(
                "INSERT OR IGNORE INTO plans (code,label,price,days,emoji,tag,sort) VALUES (?,?,?,?,?,?,?)",
                (code, label, price, days, emoji, tag, sort),
            )
        await self.conn.commit()
        await self.sync_plan_catalog()
        await self.set_default("force_channels", ",".join(DEFAULT_FORCE_CHANNELS))
        await self.set_default("start_pic", DEFAULT_START_PIC)
        await self.set_default("qr", DEFAULT_QR)
        await self.set_default("upi", DEFAULT_UPI)
        await self.set_default("tutorial", "📘 <b>Movie Bot Tutorial</b>\n\n1. Pehle required channels join karke <b>Verify</b> dabao.\n2. <b>🎬 Movies</b> par tap karo ya DM/group me movie ka naam bhejo. Spelling thodi galat ho to bhi bot closest result dhoond lega.\n3. Result me quality aur size dekhkar movie button tap karo.\n4. Movie milte hi use save ya forward kar lo, kyunki security ke liye owner ke set kiye hue time ke baad message automatically delete ho sakta hai.\n5. Movie available na ho to bot available suggestions dikhayega.\n6. Premium, coupon aur payment ke liye menu ke respective buttons use karo.\n\n⚠️ Sirf legal aur authorised content ka use karo.")
        await self.set_default("online_player_bot", "")
        await self.set_default("verify_channels_limit", "5")
        await self.set_default("caption_template", "🎬 <b>𝐌𝐨𝐯𝐢𝐞 :</b> {movie_name}\n\n🎥 <b>𝐐𝐮𝐚𝐥𝐢𝐭𝐲 :</b> {quality}\n⏱️ <b>𝐃𝐮𝐫𝐚𝐭𝐢𝐨𝐧 :</b> {duration}\n\n📢 <b>𝐉𝐨𝐢𝐧 :</b> {join}\n🌐 <b>𝐕𝐢𝐬𝐢𝐭 :</b> {username}\n\n⚡ <b>𝐄𝐍𝐣𝐨𝐲 𝐘𝐨𝐮𝐫 𝐌𝐨𝐯𝐢𝐞!</b> 🍿")
        await self.set_default("caption_join", "")
        await self.set_default("caption_visit", "")
        # Existing installs historically defaulted to 0. Migrate that legacy
        # default once, while preserving any owner-selected value thereafter.
        current_delete = await self.get("auto_delete_minutes", "")
        if not await self.get("auto_delete_default_v2", "") and current_delete in {"", "0"}:
            await self.set("auto_delete_minutes", str(DEFAULT_AUTO_DELETE_MINUTES))
        await self.set_default("auto_delete_minutes", str(DEFAULT_AUTO_DELETE_MINUTES))
        await self.set_default("support_url", "")
        await self.set_default("auto_delete_default_v2", "1")

    async def _migrate(self) -> None:
        cur = await self.conn.execute("PRAGMA table_info(plans)")
        plan_cols = {r["name"] for r in await cur.fetchall()}
        if plan_cols:
            for col, ddl in (("emoji", "TEXT DEFAULT ''"), ("tag", "TEXT DEFAULT ''"),
                             ("sort", "INTEGER DEFAULT 0")):
                if col not in plan_cols:
                    await self.conn.execute(f"ALTER TABLE plans ADD COLUMN {col} {ddl}")
        cur = await self.conn.execute("PRAGMA table_info(movies)")
        cols = {r["name"] for r in await cur.fetchall()}
        for col in ("file_id", "file_type", "duration"):
            if col not in cols:
                await self.conn.execute(f"ALTER TABLE movies ADD COLUMN {col} TEXT")
        if "storage_msg_id" not in cols:
            await self.conn.execute("ALTER TABLE movies ADD COLUMN storage_msg_id INTEGER DEFAULT 0")
        if "content_type" not in cols:
            await self.conn.execute("ALTER TABLE movies ADD COLUMN content_type TEXT DEFAULT 'movie'")
            cur = await self.conn.execute("SELECT msg_id, title, caption FROM movies")
            rows = await cur.fetchall()
            await self.conn.executemany(
                "UPDATE movies SET content_type=? WHERE msg_id=?",
                [
                    (detect_content_type(f"{row['title'] or ''} {row['caption'] or ''}"), row["msg_id"])
                    for row in rows
                ],
            )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_movies_content_type_added_at ON movies (content_type, added_at DESC)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_movies_storage_title ON movies (storage_msg_id, title)"
        )
        await self.conn.commit()

    async def schedule_expiry(self, chat_id: int, message_id: int, delete_at: int) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO delivery_expiry (chat_id,message_id,delete_at) VALUES (?,?,?)",
            (chat_id, message_id, delete_at),
        )
        await self.conn.commit()

    async def expired_deliveries(self, now: int, limit: int = 100) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT chat_id,message_id FROM delivery_expiry WHERE delete_at<=? ORDER BY delete_at LIMIT ?",
            (now, limit),
        )
        return await cur.fetchall()

    async def remove_expiry(self, chat_id: int, message_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM delivery_expiry WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
        await self.conn.commit()

    # ---------- settings ----------
    async def get(self, key: str, default: str = "") -> str:
        cur = await self.conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def set(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def commit(self) -> None:
        await self.conn.commit()

    async def rollback(self) -> None:
        await self.conn.rollback()

    async def close(self) -> None:
        conn = getattr(self, "conn", None)
        if conn is not None:
            await conn.close()

    async def export_state(self) -> dict:
        tables = ("users", "movies", "history", "plans", "coupons", "coupon_uses", "admins", "settings", "stats", "payments", "audit_logs", "delivery_expiry")
        state = {"version": 1, "tables": {}}
        for table in tables:
            cur = await self.conn.execute(f"SELECT * FROM {table}")
            state["tables"][table] = [dict(row) for row in await cur.fetchall()]
        return state

    async def export_tables(self, table_names: tuple[str, ...]) -> dict:
        """Export only the specified tables — avoids loading 1M+ movies when
        we only need users/settings for a MongoDB state save."""
        state = {"version": 1, "tables": {}}
        for table in table_names:
            if table not in {"users", "movies", "history", "plans", "coupons", "coupon_uses", "admins", "settings", "stats", "payments", "audit_logs", "delivery_expiry"}:
                continue
            cur = await self.conn.execute(f"SELECT * FROM {table}")
            state["tables"][table] = [dict(row) for row in await cur.fetchall()]
        return state

    async def export_movie_catalog(self, batch_size: int = 5000) -> list[dict]:
        """Export movies in batches to avoid loading 1M+ rows into memory at once."""
        rows = []
        offset = 0
        while True:
            cur = await self.conn.execute(
                "SELECT * FROM movies ORDER BY msg_id LIMIT ? OFFSET ?", (batch_size, offset)
            )
            batch = [dict(row) for row in await cur.fetchall()]
            if not batch:
                break
            rows.extend(batch)
            offset += batch_size
        return rows

    async def export_movie_catalog_batch(self, after_msg_id: int = 0, limit: int = 5000) -> list[dict]:
        """Return one keyset-paginated catalog batch without loading the full catalog."""
        cur = await self.conn.execute(
            "SELECT * FROM movies WHERE msg_id > ? ORDER BY msg_id LIMIT ?",
            (after_msg_id, limit),
        )
        rows = []
        for row in await cur.fetchall():
            item = dict(row)
            # Only lightweight metadata leaves for MongoDB — never media bytes.
            if MONGO_CAPTION_LIMIT and isinstance(item.get("caption"), str):
                item["caption"] = item["caption"][:MONGO_CAPTION_LIMIT]
            rows.append(item)
        return rows

    async def bulk_upsert_movies(self, rows: list[tuple]) -> int:
        """Fast path for big scans: one executemany instead of per-row logic."""
        if not rows:
            return 0
        await self.conn.executemany(
            """INSERT INTO movies (msg_id,title,year,quality,language,size,duration,caption,file_id,file_type,storage_msg_id,content_type,added_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(msg_id) DO UPDATE SET title=excluded.title, year=excluded.year,
                 quality=excluded.quality, language=excluded.language, size=excluded.size,
                 caption=excluded.caption,
                 duration=CASE WHEN excluded.duration != '' THEN excluded.duration ELSE movies.duration END,
                 file_id=CASE WHEN excluded.file_id != '' THEN excluded.file_id ELSE movies.file_id END,
                 file_type=CASE WHEN excluded.file_type != '' THEN excluded.file_type ELSE movies.file_type END,
                 storage_msg_id=CASE WHEN excluded.storage_msg_id > 0 THEN excluded.storage_msg_id ELSE movies.storage_msg_id END,
                 content_type=excluded.content_type""",
            rows,
        )
        return len(rows)

    async def import_state(self, state: dict) -> None:
        tables = state.get("tables", {}) if isinstance(state, dict) else {}
        try:
            await self.conn.execute("BEGIN")
            for table, rows in tables.items():
                if table not in {"users", "movies", "history", "plans", "coupons", "coupon_uses", "admins", "settings", "stats", "payments", "audit_logs", "delivery_expiry"}:
                    continue
                cur = await self.conn.execute(f"PRAGMA table_info({table})")
                columns = {row["name"] for row in await cur.fetchall()}
                for row in rows:
                    values = {key: value for key, value in row.items() if key in columns}
                    if not values:
                        continue
                    names = list(values)
                    placeholders = ",".join("?" for _ in names)
                    await self.conn.execute(
                        f"INSERT OR REPLACE INTO {table} ({','.join(names)}) VALUES ({placeholders})",
                        tuple(values[name] for name in names),
                    )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise

    async def is_empty_index(self) -> bool:
        return (await self.movie_count()) == 0

    async def set_default(self, key: str, value: str) -> None:
        cur = await self.conn.execute("SELECT 1 FROM settings WHERE key=?", (key,))
        if not await cur.fetchone():
            await self.set(key, value)

    # ---------- users ----------
    async def add_user(self, user_id: int, username: str, name: str, referred_by: int = 0) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        if await cur.fetchone():
            return False
        await self.conn.execute(
            "INSERT INTO users (user_id,username,name,joined_at,referred_by) VALUES (?,?,?,?,?)",
            (user_id, username, name, int(time.time()), referred_by),
        )
        if referred_by:
            await self.conn.execute("UPDATE users SET referrals=referrals+1 WHERE user_id=?", (referred_by,))
        await self.conn.commit()
        return True

    async def user(self, user_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return await cur.fetchone()

    async def update_user(self, user_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - self._USER_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported user fields: {', '.join(sorted(unknown))}")
        cols = ",".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE users SET {cols} WHERE user_id=?", (*fields.values(), user_id))
        await self.conn.commit()

    async def all_user_ids(self) -> list[int]:
        cur = await self.conn.execute("SELECT user_id FROM users WHERE banned=0")
        return [r["user_id"] for r in await cur.fetchall()]

    async def user_count(self, include_banned: bool = False) -> int:
        query = "SELECT COUNT(*) c FROM users" if include_banned else "SELECT COUNT(*) c FROM users WHERE banned=0"
        cur = await self.conn.execute(query)
        return (await cur.fetchone())["c"]

    async def premium_users(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE premium_until > ? ORDER BY premium_until DESC", (int(time.time()),)
        )
        return await cur.fetchall()

    async def add_premium(self, user_id: int, days: float) -> int:
        row = await self.user(user_id)
        now = int(time.time())
        base = max(now, row["premium_until"] if row else 0)
        until = int(base + days * 86400)
        await self.conn.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until, user_id))
        await self.conn.commit()
        return until

    # ---------- movies ----------
    async def upsert_movie(self, msg_id: int, title: str, year: str, quality: str,
                           language: str, size: str, caption: str,
                           file_id: str = "", file_type: str = "", content_type: str = "movie",
                           duration: str = "",
                           storage_msg_id: int = 0, commit: bool = True) -> bool:
        safe_type = content_type if content_type in {"movie", "series", "anime"} else "movie"
        if file_id:
            cur = await self.conn.execute(
                "SELECT msg_id, storage_msg_id FROM movies WHERE file_id=? AND msg_id<>? LIMIT 1",
                (file_id, msg_id),
            )
            duplicate = await cur.fetchone()
            if duplicate:
                if storage_msg_id > 0 and not duplicate["storage_msg_id"]:
                    await self.conn.execute(
                        "UPDATE movies SET msg_id=?, storage_msg_id=?, title=?, year=?, quality=?, language=?, size=?, duration=?, caption=?, file_type=?, content_type=? WHERE msg_id=?",
                        (msg_id, storage_msg_id, title, year, quality, language, size, duration, caption, file_type, safe_type, duplicate["msg_id"]),
                    )
                    if commit:
                        await self.conn.commit()
                    return True
                return False
        await self.conn.execute(
            """INSERT INTO movies (msg_id,title,year,quality,language,size,duration,caption,file_id,file_type,storage_msg_id,content_type,added_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(msg_id) DO UPDATE SET title=excluded.title, year=excluded.year,
                 quality=excluded.quality, language=excluded.language, size=excluded.size,
                 caption=excluded.caption,
                 duration=CASE WHEN excluded.duration != '' THEN excluded.duration ELSE movies.duration END,
                 file_id=CASE WHEN excluded.file_id != '' THEN excluded.file_id ELSE movies.file_id END,
                 file_type=CASE WHEN excluded.file_type != '' THEN excluded.file_type ELSE movies.file_type END,
                 storage_msg_id=CASE WHEN excluded.storage_msg_id > 0 THEN excluded.storage_msg_id ELSE movies.storage_msg_id END,
                 content_type=excluded.content_type""",
            (msg_id, title, year, quality, language, size, duration, caption, file_id, file_type, storage_msg_id, safe_type, int(time.time())),
        )
        if commit:
            await self.conn.commit()
        return True

    async def has_duplicate_movie(self, file_id: str = "", caption: str = "") -> bool:
        """Check whether a deliverable movie already exists by file or exact caption.

        Uses the same whitespace+casefold normalization as get_dedup_sets so that
        live auto-transfer and scan-time dedup agree on what counts as a duplicate."""
        file_id = (file_id or "").strip()
        caption_norm = " ".join((caption or "").split()).strip().casefold()
        if file_id:
            cur = await self.conn.execute(
                "SELECT 1 FROM movies WHERE storage_msg_id > 0 AND file_id=? LIMIT 1", (file_id,)
            )
            if await cur.fetchone():
                return True
        if caption_norm:
            cur = await self.conn.execute(
                "SELECT caption FROM movies WHERE storage_msg_id > 0 AND caption IS NOT NULL"
            )
            async for row in cur:
                stored = " ".join(str(row["caption"]).split()).strip().casefold()
                if stored == caption_norm:
                    return True
        return False

    async def search_movies_sql(self, query: str, content_type: str = "", limit: int = 20) -> list[aiosqlite.Row]:
        """SQL-based pre-filter for fast search on large catalogs."""
        safe_type = content_type if content_type in {"movie", "series", "anime"} else ""
        q = f"%{query}%"
        if safe_type:
            cur = await self.conn.execute(
                "SELECT * FROM movies WHERE storage_msg_id > 0 AND content_type=? AND LOWER(title) LIKE LOWER(?) LIMIT ?",
                (safe_type, q, limit * 3),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM movies WHERE storage_msg_id > 0 AND LOWER(title) LIKE LOWER(?) LIMIT ?",
                (q, limit * 3),
            )
        return await cur.fetchall()

    async def search_titles_for_spelling(self, query: str, limit: int = 200) -> list[str]:
        """Return candidate titles for spelling correction without loading all rows."""
        q = f"%{query[:3]}%" if len(query) >= 3 else f"%{query}%"
        cur = await self.conn.execute(
            "SELECT title FROM movies WHERE storage_msg_id > 0 AND LOWER(title) LIKE LOWER(?) LIMIT ?",
            (q, limit),
        )
        return [str(row["title"] or "") for row in await cur.fetchall() if row["title"]]

    async def ban_user(self, user_id: int) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        if not await cur.fetchone():
            return False
        await self.conn.execute("UPDATE users SET banned=1 WHERE user_id=?", (user_id,))
        await self.conn.commit()
        return True

    async def unban_user(self, user_id: int) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        if not await cur.fetchone():
            return False
        await self.conn.execute("UPDATE users SET banned=0 WHERE user_id=?", (user_id,))
        await self.conn.commit()
        return True

    async def all_movies(self, storage_only: bool = False) -> list[aiosqlite.Row]:
        query = "SELECT * FROM movies WHERE storage_msg_id > 0" if storage_only else "SELECT * FROM movies"
        cur = await self.conn.execute(query)
        return await cur.fetchall()

    async def get_dedup_sets(self) -> tuple[set[str], set[str]]:
        """Return (file_ids, normalized_captions) for dedup during scanning.

        Uses SQL to fetch only the two columns needed instead of full rows.
        For 100k+ movies this is ~10x lighter than all_movies()."""
        cur = await self.conn.execute(
            "SELECT file_id, caption FROM movies WHERE storage_msg_id > 0"
        )
        files: set[str] = set()
        captions: set[str] = set()
        async for row in cur:
            fid = row["file_id"]
            if fid:
                files.add(str(fid))
            cap = row["caption"]
            if cap:
                captions.add(" ".join(str(cap).split()).strip().casefold())
        return files, captions

    async def bulk_check_duplicates(self, items: list[dict]) -> list[dict]:
        """Batch-check a list of {file_key, caption_key} dicts against the DB.

        Returns only the items that are NOT duplicates. Uses a single SQL
        query per batch instead of one query per item."""
        if not items:
            return []
        file_keys = [i["file_key"] for i in items if i.get("file_key")]
        existing_files: set[str] = set()
        if file_keys:
            placeholders = ",".join("?" * len(file_keys))
            cur = await self.conn.execute(
                f"SELECT file_id FROM movies WHERE storage_msg_id > 0 AND file_id IN ({placeholders})",
                file_keys,
            )
            existing_files = {str(row["file_id"]) for row in await cur.fetchall()}

        caption_keys = [i["caption_key"] for i in items if i.get("caption_key")]
        existing_captions: set[str] = set()
        if caption_keys:
            # Normalize stored captions the same way as the check side.
            cur = await self.conn.execute(
                "SELECT caption FROM movies WHERE storage_msg_id > 0 AND caption IS NOT NULL"
            )
            async for row in cur:
                cap = row["caption"]
                if cap:
                    existing_captions.add(" ".join(str(cap).split()).strip().casefold())

        result = []
        for item in items:
            fk = item.get("file_key", "")
            ck = item.get("caption_key", "")
            if fk and fk in existing_files:
                continue
            if ck and ck in existing_captions:
                continue
            result.append(item)
        return result

    async def movie(self, msg_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM movies WHERE msg_id=?", (msg_id,))
        return await cur.fetchone()

    async def movie_count(self, storage_only: bool = False) -> int:
        query = "SELECT COUNT(*) c FROM movies WHERE storage_msg_id > 0" if storage_only else "SELECT COUNT(*) c FROM movies"
        cur = await self.conn.execute(query)
        return (await cur.fetchone())["c"]

    async def delete_movie(self, msg_id: int) -> None:
        await self.conn.execute("DELETE FROM movies WHERE msg_id=?", (msg_id,))
        await self.conn.commit()

    async def delete_movie_by_storage_id(self, storage_msg_id: int) -> None:
        await self.conn.execute("DELETE FROM movies WHERE storage_msg_id=? OR msg_id=?", (storage_msg_id, storage_msg_id))
        await self.conn.commit()

    async def add_audit(self, user_id: int, event: str, details: str = "") -> None:
        await self.conn.execute(
            "INSERT INTO audit_logs (user_id,event,details,at) VALUES (?,?,?,?)",
            (user_id, event, details[:1000], int(time.time())),
        )
        await self.conn.commit()

    async def popular_movies(self, content_type: str = "", limit: int = 10) -> list[aiosqlite.Row]:
        """Rank deliverable movies by successful deliveries and unique users."""
        safe_limit = max(1, min(int(limit), 15))
        safe_type = content_type if content_type in {"movie", "series", "anime"} else ""
        type_clause = " AND m.content_type=?" if safe_type else ""
        params = (safe_type, safe_limit) if safe_type else (safe_limit,)
        cur = await self.conn.execute(
            f"""SELECT m.*, COUNT(h.id) AS request_count,
                         COUNT(DISTINCT h.user_id) AS unique_users,
                         MAX(h.at) AS last_requested_at
               FROM movies AS m
               INNER JOIN history AS h ON h.msg_id = m.msg_id
               WHERE m.storage_msg_id > 0{type_clause}
               GROUP BY m.msg_id
               ORDER BY request_count DESC, unique_users DESC,
                        last_requested_at DESC, m.added_at DESC, m.msg_id DESC
               LIMIT ?""",
            params,
        )
        return await cur.fetchall()

    async def recent_movies(self, content_type: str = "", limit: int = 10,
                            storage_only: bool = False) -> list[aiosqlite.Row]:
        """Return recent catalog rows, optionally restricted to deliverable storage posts."""
        safe_limit = max(1, min(int(limit), 15))
        safe_type = content_type if content_type in {"movie", "series", "anime"} else ""
        filter_sql = "WHERE m.storage_msg_id > 0" if storage_only else "WHERE 1=1"
        if safe_type:
            filter_sql += " AND m.content_type=?"
        params = (safe_type, safe_limit) if safe_type else (safe_limit,)
        cur = await self.conn.execute(
            f"""SELECT m.*, 0 AS request_count
               FROM movies AS m
               {filter_sql}
               ORDER BY m.added_at DESC, m.msg_id DESC
               LIMIT ?""",
            params,
        )
        return await cur.fetchall()

    # ---------- history ----------
    async def add_history(self, user_id: int, query: str, title: str, msg_id: int) -> None:
        await self.conn.execute(
            "INSERT INTO history (user_id,query,title,msg_id,at) VALUES (?,?,?,?,?)",
            (user_id, query, title, msg_id, int(time.time())),
        )
        await self.conn.commit()

    async def history(self, user_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )
        return await cur.fetchall()

    # ---------- plans / coupons / admins ----------
    async def plans(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM plans ORDER BY CASE WHEN COALESCE(sort,0)=0 THEN 1 ELSE 0 END, sort, days"
        )
        return await cur.fetchall()

    async def sync_plan_catalog(self) -> None:
        """Keep the plan catalog aligned with DEFAULT_PLANS.

        Labels, emojis, badges, duration and ordering always follow the code;
        prices are owner-controlled and only reset when PLAN_PRICING_REVISION
        changes (so a restored old MongoDB state cannot bring back old prices).
        """
        reset_prices = (await self.get("plan_pricing_revision", "")) != PLAN_PRICING_REVISION
        for code, label, price, days, emoji, tag, sort in DEFAULT_PLANS:
            cur = await self.conn.execute("SELECT price FROM plans WHERE code=?", (code,))
            row = await cur.fetchone()
            if row is None:
                await self.conn.execute(
                    "INSERT INTO plans (code,label,price,days,emoji,tag,sort) VALUES (?,?,?,?,?,?,?)",
                    (code, label, price, days, emoji, tag, sort),
                )
                continue
            keep_price = price if reset_prices else row["price"]
            await self.conn.execute(
                "UPDATE plans SET label=?, price=?, days=?, emoji=?, tag=?, sort=? WHERE code=?",
                (label, keep_price, days, emoji, tag, sort, code),
            )
        await self.conn.commit()
        if reset_prices:
            await self.set("plan_pricing_revision", PLAN_PRICING_REVISION)

    async def set_plan_tag(self, code: str, tag: str) -> None:
        await self.conn.execute("UPDATE plans SET tag=? WHERE code=?", (tag, code))
        await self.conn.commit()

    async def plan(self, code: str) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM plans WHERE code=?", (code,))
        return await cur.fetchone()

    async def set_plan_price(self, code: str, price: int) -> None:
        await self.conn.execute("UPDATE plans SET price=? WHERE code=?", (price, code))
        await self.conn.commit()

    async def add_coupon(self, code: str, days: int, max_uses: int) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO coupons (code,days,max_uses,used) VALUES (?,?,?,0)",
            (code.upper(), days, max_uses),
        )
        await self.conn.commit()

    async def redeem_coupon(self, code: str, user_id: int) -> Optional[int]:
        """Redeem once per user and reserve one use atomically under concurrency."""
        code = "".join((code or "").upper().split())
        if not code:
            return None
        try:
            await self.conn.execute("BEGIN IMMEDIATE")
            cur = await self.conn.execute("SELECT days FROM coupons WHERE code=? AND used < max_uses", (code,))
            row = await cur.fetchone()
            if not row:
                await self.conn.rollback()
                return None
            cur = await self.conn.execute("SELECT 1 FROM coupon_uses WHERE code=? AND user_id=?", (code, user_id))
            if await cur.fetchone():
                await self.conn.rollback()
                return None
            await self.conn.execute("UPDATE coupons SET used=used+1 WHERE code=? AND used < max_uses", (code,))
            await self.conn.execute("INSERT INTO coupon_uses (code,user_id) VALUES (?,?)", (code, user_id))
            await self.conn.commit()
            return row["days"]
        except Exception:
            await self.conn.rollback()
            return None

    async def admins(self) -> list[int]:
        cur = await self.conn.execute("SELECT user_id FROM admins")
        return [r["user_id"] for r in await cur.fetchall()]

    async def add_admin(self, user_id: int) -> None:
        await self.conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        await self.conn.commit()

    async def remove_admin(self, user_id: int) -> None:
        await self.conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await self.conn.commit()

    async def add_payment(self, user_id: int, plan: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO payments (user_id,plan,at) VALUES (?,?,?)", (user_id, plan, int(time.time()))
        )
        await self.conn.commit()
        return cur.lastrowid or 0

    async def payment(self, pid: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM payments WHERE id=?", (pid,))
        return await cur.fetchone()

    async def set_payment_status(self, pid: int, status: str) -> None:
        await self.conn.execute("UPDATE payments SET status=? WHERE id=?", (status, pid))
        await self.conn.commit()

    async def approve_payment(self, pid: int, user_id: int, days: float) -> Optional[int]:
        """Approve one pending payment and grant its premium in one transaction."""
        try:
            await self.conn.execute("BEGIN IMMEDIATE")
            payment_cur = await self.conn.execute(
                "SELECT status, user_id FROM payments WHERE id=?", (pid,)
            )
            payment = await payment_cur.fetchone()
            if not payment or payment["status"] != "pending" or int(payment["user_id"]) != int(user_id):
                await self.conn.rollback()
                return None
            user_cur = await self.conn.execute(
                "SELECT premium_until FROM users WHERE user_id=?", (user_id,)
            )
            user = await user_cur.fetchone()
            if not user:
                await self.conn.rollback()
                return None
            now = int(time.time())
            until = int(max(now, user["premium_until"] or 0) + float(days) * 86400)
            await self.conn.execute(
                "UPDATE users SET premium_until=? WHERE user_id=?", (until, user_id)
            )
            await self.conn.execute(
                "UPDATE payments SET status='approved' WHERE id=? AND status='pending'", (pid,)
            )
            await self.conn.commit()
            return until
        except Exception:
            await self.conn.rollback()
            return None


    async def ensure_stats(self):
        await self.conn.execute("CREATE TABLE IF NOT EXISTS stats(key TEXT PRIMARY KEY, count INTEGER)")
        await self.conn.commit()

    async def incr_stat(self, key: str) -> int:
        try:
            await self.conn.execute(
                "INSERT INTO stats(key, count) VALUES(?, 1) ON CONFLICT(key) DO UPDATE SET count = count + 1",
                (key,)
            )
            await self.conn.commit()
            cur = await self.conn.execute("SELECT count FROM stats WHERE key = ?", (key,))
            row = await cur.fetchone()
            return int(row[0]) if row else 1
        except Exception:
            return 0

db = DB()
