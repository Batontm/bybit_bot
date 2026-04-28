"""
SlippageGuard — учёт превышений slippage и временный бан пар.

Принцип работы:
1. На каждое OPEN/DCA с превышением `MAX_SLIPPAGE_PERCENT` контроллер
   вызывает `await guard.handle(pair, slippage, action)`.
2. Guard записывает событие в таблицу `slippage_events` и считает,
   сколько превышений было за последние 24 часа.
3. Если >= `BAN_THRESHOLD` — пара заносится в `settings` с TTL 24ч,
   и в Telegram идёт алерт.
4. `is_pair_banned(pair)` используется при формировании списка активных пар.

Все обращения к БД защищены try/except — падение этой подсистемы
не должно ронять торгового бота.
"""
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from config.settings import TIMEZONE, logger

from ..db.connection import db


NotifyCallback = Callable[[str], Awaitable[None]]


class SlippageGuard:
    """Защита от пар с систематическим высоким slippage."""

    DEFAULT_BAN_THRESHOLD = 3   # превышений за 24ч
    DEFAULT_BAN_HOURS = 24

    def __init__(self,
                 notifier: Optional[NotifyCallback] = None,
                 ban_threshold: int = DEFAULT_BAN_THRESHOLD,
                 ban_hours: int = DEFAULT_BAN_HOURS):
        self._notifier = notifier
        self.ban_threshold = ban_threshold
        self.ban_hours = ban_hours

    def set_notifier(self, notifier: NotifyCallback) -> None:
        self._notifier = notifier

    # -------- public API --------

    def is_pair_banned(self, pair: str) -> bool:
        """Действует ли активный 24-часовой бан на пару."""
        try:
            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT value FROM settings WHERE key = ?",
                    (self._ban_key(pair),),
                )
                row = cursor.fetchone()
                if not row or not row[0]:
                    return False
                ban_until = datetime.fromisoformat(row[0])
                if ban_until.tzinfo is None:
                    ban_until = ban_until.replace(tzinfo=TIMEZONE)
                return datetime.now(TIMEZONE) < ban_until
        except Exception:
            return False

    def record_event(self, pair: str, action: str, slippage: float) -> int:
        """Записать событие; вернуть количество превышений за 24ч (включая текущее)."""
        try:
            now = datetime.now(TIMEZONE)
            cutoff = (now - timedelta(hours=24)).isoformat()
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO slippage_events (pair, action, slippage, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (pair, action, float(slippage), now.isoformat()),
                )
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM slippage_events "
                    "WHERE pair = ? AND created_at >= ?",
                    (pair, cutoff),
                )
                row = cursor.fetchone()
                return int(row[0] if row else 0)
        except Exception:
            return 0

    async def handle(self, pair: str, slippage: float, action: str) -> bool:
        """Записать превышение и при необходимости забанить пару.

        Returns: True если пара была забанена этим вызовом.
        """
        exceed_count = self.record_event(pair, action, slippage)
        if exceed_count >= self.ban_threshold:
            return await self._ban_pair(pair, exceed_count)
        return False

    # -------- internal --------

    @staticmethod
    def _ban_key(pair: str) -> str:
        return f"ban_pair_{pair}"

    async def _ban_pair(self, pair: str, exceed_count_24h: int) -> bool:
        if self.is_pair_banned(pair):
            return False

        ban_until = datetime.now(TIMEZONE) + timedelta(hours=self.ban_hours)
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (self._ban_key(pair), ban_until.isoformat()),
                )
        except Exception as e:
            logger.error(f"❌ SlippageGuard: не удалось записать бан {pair}: {e}")
            return False

        if self._notifier:
            try:
                await self._notifier(
                    f"⛔️ {pair} заблокирована на {self.ban_hours}ч из-за slippage "
                    f"(превышений за 24ч: {exceed_count_24h})\n"
                    f"До: {ban_until.isoformat()}"
                )
            except Exception:
                pass

        return True
