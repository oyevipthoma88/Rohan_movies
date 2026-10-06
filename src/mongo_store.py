"""MongoDB persistence with DATA LOSS PREVENTION.
CRITICAL: Never overwrites with fewer users. Recovers from backups.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config import MONGO_DB, MONGO_URI
from db import db

PERSISTED_TABLES = (
    "users", "history", "plans", "coupons", "coupon_uses", "admins",
    "settings", "stats", "payments", "audit_logs",
)
NON_MOVIE_TABLES = PERSISTED_TABLES
# Movie catalog is also persisted so the bot survives restarts without a full rescan.
MOVIE_TABLES = ("movies",)

_client = None
_collection = None
_save_lock = asyncio.Lock()
MOVIE_CHUNK_SIZE = 5000


async def _collection_or_none():
    global _client, _collection
    if not MONGO_URI:
        return None
    if _collection is None:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            _client = AsyncIOMotorClient(
                MONGO_URI,
                serverSelectionTimeoutMS=10000,
                connectTimeoutMS=10000,
                socketTimeoutMS=30000,
                maxPoolSize=20,
                minPoolSize=2
            )
            _collection = _client[MONGO_DB]["bot_state"]
            try:
                await asyncio.wait_for(_client.admin.command("ping"), timeout=12.0)
                logging.info("MongoDB connected")
            except asyncio.TimeoutError:
                logging.warning("MongoDB timeout")
                _client.close()
                _client = None
                _collection = None
                return None
            except Exception as e:
                logging.warning(f"MongoDB ping failed: {e}")
                _client.close()
                _client = None
                _collection = None
                return None
        except Exception as e:
            logging.warning(f"MongoDB connection failed: {e}")
            _client = None
            _collection = None
            return None
    return _collection


async def restore_non_movie_state():
    """Restore from MongoDB."""
    try:
        collection = await _collection_or_none()
        if collection is None:
            return False
        document = await collection.find_one({"_id": "state"})
        if not document:
            return False
        tables = {name: document.get("tables", {}).get(name, []) for name in NON_MOVIE_TABLES}
        await db.import_state({"version": 2, "tables": tables})
        user_count = await db.user_count()
        logging.info(f"Mongo restored: users={user_count}")
        return True
    except Exception:
        logging.exception("Mongo restore failed")
        return False


async def restore_movie_catalog():
    """Restore movie catalog from MongoDB so stats survive restarts."""
    try:
        collection = await _collection_or_none()
        if collection is None:
            return False
        document = await collection.find_one({"_id": "movies"})
        if not document:
            return False
        if document.get("chunked"):
            chunk_count = max(0, int(document.get("chunk_count", 0)))
            generation = str(document.get("generation", ""))
            restored_any = False
            for offset in range(0, chunk_count, 20):
                ids = [
                    f"movies:{generation}:{index}" if generation else f"movies:{index}"
                    for index in range(offset, min(offset + 20, chunk_count))
                ]
                chunks = await collection.find({"_id": {"$in": ids}}).to_list(length=len(ids))
                by_id = {chunk["_id"]: chunk for chunk in chunks}
                for chunk_id in ids:
                    rows = by_id.get(chunk_id, {}).get("rows", [])
                    if rows:
                        await db.import_state({"version": 3, "tables": {"movies": rows}})
                        restored_any = True
            if not restored_any and int(document.get("count", 0)):
                logging.warning("Mongo movie manifest exists but its chunks are missing")
                return False
        else:
            # Backward compatibility with the old single-document layout.
            rows = document.get("rows", [])
            if not rows:
                return False
            for offset in range(0, len(rows), 2000):
                await db.import_state({"version": 2, "tables": {"movies": rows[offset:offset + 2000]}})
        movie_count = await db.movie_count()
        logging.info(f"Mongo restored movie catalog: {movie_count} movies")
        return True
    except Exception:
        logging.exception("Mongo movie catalog restore failed")
        return False


async def save_movie_catalog():
    """Save movie catalog to MongoDB in batches to avoid OOM on large catalogs."""
    try:
        collection = await _collection_or_none()
        if collection is None:
            return False
        now = datetime.now(timezone.utc)
        generation = now.strftime("%Y%m%dT%H%M%S%f")
        previous = await collection.find_one({"_id": "movies"}, {"generation": 1, "chunk_count": 1})
        chunk_count = 0
        row_count = 0
        after_msg_id = 0
        while True:
            chunk = await db.export_movie_catalog_batch(after_msg_id, MOVIE_CHUNK_SIZE)
            if not chunk:
                break
            await asyncio.wait_for(
                collection.replace_one(
                    {"_id": f"movies:{generation}:{chunk_count}"},
                    {"_id": f"movies:{generation}:{chunk_count}", "version": 3,
                     "generation": generation, "updated_at": now, "rows": chunk},
                    upsert=True,
                ),
                timeout=30.0,
            )
            chunk_count += 1
            row_count += len(chunk)
            after_msg_id = int(chunk[-1]["msg_id"])
        # Publish the manifest last, so restore never observes a partially written generation.
        await collection.replace_one(
            {"_id": "movies"},
            {"_id": "movies", "version": 3, "updated_at": now, "count": row_count,
             "chunked": True, "generation": generation, "chunk_count": chunk_count},
            upsert=True,
        )
        old_generation = str((previous or {}).get("generation", ""))
        old_chunk_count = int((previous or {}).get("chunk_count", 0))
        if old_chunk_count:
            old_ids = [
                f"movies:{old_generation}:{i}" if old_generation else f"movies:{i}"
                for i in range(old_chunk_count)
            ]
            await collection.delete_many({"_id": {"$in": old_ids}})
        logging.info(f"Mongo saved movie catalog: {row_count} movies in {chunk_count} chunks")
        return True
    except asyncio.TimeoutError:
        logging.warning("Mongo movie catalog save timeout")
        return False
    except Exception as e:
        logging.warning(f"Mongo movie catalog save error: {type(e).__name__}: {e}")
        return False


async def save_non_movie_state():
    """Save with DATA LOSS PREVENTION - never reduces user count."""
    try:
        async with _save_lock:
            collection = await _collection_or_none()
            if collection is None:
                return False
            
            current_users = await db.user_count(include_banned=True)
            
            # CRITICAL SAFETY CHECK: Fetch existing data
            existing_doc = None
            try:
                existing_doc = await asyncio.wait_for(
                    collection.find_one({"_id": "state"}),
                    timeout=10.0
                )
            except Exception:
                pass
            
            if existing_doc:
                old_tables = existing_doc.get("tables", {})
                stored_users = len(old_tables.get("users", []))
                
                # DATA LOSS PREVENTION: Never save if we have FEWER users
                if current_users < stored_users:
                    logging.warning(
                        f"DATA PROTECTION: Not saving! "
                        f"Current users ({current_users}) < Stored users ({stored_users}). "
                        f"Preventing data loss!"
                    )
                    return False
                
                # Merge users (keep all)
                state = await db.export_tables(NON_MOVIE_TABLES)
                new_tables = {name: state["tables"].get(name, []) for name in NON_MOVIE_TABLES}
                
                # Merge user lists
                old_user_list = old_tables.get("users", [])
                new_user_list = new_tables.get("users", [])
                
                user_dict = {}
                for u in old_user_list:
                    uid = u.get("user_id")
                    if uid:
                        user_dict[uid] = u
                for u in new_user_list:
                    uid = u.get("user_id")
                    if uid:
                        user_dict[uid] = u
                
                final_tables = dict(new_tables)
                final_tables["users"] = list(user_dict.values())

                logging.info(f"Mongo save: users merged {stored_users}+{current_users}->{len(final_tables['users'])}")
            else:
                state = await db.export_tables(NON_MOVIE_TABLES)
                final_tables = {name: state["tables"].get(name, []) for name in NON_MOVIE_TABLES}
            
            await asyncio.wait_for(
                collection.replace_one(
                    {"_id": "state"},
                    {"_id": "state", "version": 2, "updated_at": datetime.now(timezone.utc), "tables": final_tables},
                    upsert=True,
                ),
                timeout=30.0
            )
            return True
    except asyncio.TimeoutError:
        logging.warning("Mongo save timeout")
        return False
    except Exception as e:
        logging.warning(f"Mongo save error: {type(e).__name__}")
        return False


async def close_mongo():
    global _client, _collection
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None
    _collection = None


__all__ = ["PERSISTED_TABLES", "NON_MOVIE_TABLES", "MOVIE_TABLES", "restore_non_movie_state", "save_non_movie_state", "restore_movie_catalog", "save_movie_catalog", "close_mongo"]
