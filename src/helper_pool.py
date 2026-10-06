"""
Multi-Bot Transfer System - Helper Bot Pool Manager
Manages multiple helper bots for parallel file transfers.
Main bot NEVER transfers files - only helper bots do.
"""
import asyncio
import logging
from typing import Optional, Dict, List
from dataclasses import dataclass, field
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import HELPER_BOT_TOKENS

logger = logging.getLogger(__name__)


@dataclass
class HelperBot:
    token: str
    bot: Optional[Bot] = None
    bot_id: int = 0
    username: str = ""
    is_active: bool = True
    transfers_done: int = 0
    transfers_failed: int = 0
    last_error: str = ""
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def initialize(self) -> bool:
        try:
            self.bot = Bot(token=self.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            me = await self.bot.get_me()
            self.bot_id = me.id
            self.username = me.username or f"bot_{self.bot_id}"
            self.is_active = True
            logger.info(f"Helper bot initialized: @{self.username} (ID: {self.bot_id})")
            return True
        except Exception as e:
            logger.error(f"Failed to init helper bot {self.token[:10]}...: {e}")
            self.is_active = False
            self.last_error = str(e)
            return False

    async def close(self):
        if self.bot:
            try:
                await self.bot.session.close()
            except Exception:
                pass

    @property
    def success_rate(self) -> float:
        total = self.transfers_done + self.transfers_failed
        return self.transfers_done / total if total > 0 else 1.0


class HelperBotPool:
    def __init__(self):
        self._bots: List[HelperBot] = []
        self._current_index: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._initialized: bool = False

    @property
    def active_bots(self) -> List[HelperBot]:
        return [b for b in self._bots if b.is_active and b.bot]

    @property
    def total_bots(self) -> int:
        return len(self._bots)

    @property
    def active_count(self) -> int:
        return len(self.active_bots)

    async def initialize(self) -> int:
        if self._initialized:
            return self.active_count
        tokens = [t.strip() for t in HELPER_BOT_TOKENS.split(",") if t.strip()]
        if not tokens:
            logger.warning("No helper bot tokens configured.")
            return 0
        logger.info(f"Initializing {len(tokens)} helper bots...")
        for i, token in enumerate(tokens):
            helper = HelperBot(token=token)
            if await helper.initialize():
                self._bots.append(helper)
            else:
                logger.warning(f"Skipping helper #{i+1}")
        self._initialized = True
        logger.info(f"Helper pool ready: {self.active_count}/{self.total_bots} active")
        return self.active_count

    async def get_next_bot(self) -> Optional[HelperBot]:
        async with self._lock:
            active = self.active_bots
            if not active:
                return None
            bot = active[self._current_index % len(active)]
            self._current_index += 1
            return bot

    async def mark_success(self, bot: HelperBot):
        async with bot._lock:
            bot.transfers_done += 1

    async def mark_failure(self, bot: HelperBot, error: str = ""):
        async with bot._lock:
            bot.transfers_failed += 1
            bot.last_error = error[:200]
            if bot.transfers_failed > 10 and bot.success_rate < 0.3:
                bot.is_active = False
                logger.warning(f"Bot @{bot.username} deactivated (high failure rate)")

    async def reactivate_bots(self):
        for bot in self._bots:
            if not bot.is_active:
                if await bot.initialize():
                    logger.info(f"Bot @{bot.username} reactivated")

    def get_stats(self) -> Dict:
        return {
            "total": self.total_bots,
            "active": self.active_count,
            "bots": [{"username": b.username, "id": b.bot_id, "active": b.is_active,
                      "done": b.transfers_done, "failed": b.transfers_failed,
                      "rate": f"{b.success_rate*100:.1f}%", "error": b.last_error[:50]}
                     for b in self._bots]
        }

    async def close_all(self):
        for bot in self._bots:
            await bot.close()
        self._bots.clear()
        self._initialized = False

helper_pool = HelperBotPool()
