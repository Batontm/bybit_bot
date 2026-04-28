"""Тесты SlippageGuard.

Используем in-memory подмену singleton'а БД, чтобы не трогать реальный bot.db.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from config.settings import TIMEZONE
from bot.services import slippage_guard as sg_module
from bot.services.slippage_guard import SlippageGuard


class _FakeDB:
    """Минимальная замена `bot.db.connection.db` для тестов."""

    def __init__(self):
        # ":memory:" нельзя — нужен общий между методами, поэтому файл? нет,
        # достаточно одного persistent in-memory соединения.
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)
        """)
        self._conn.execute("""
            CREATE TABLE slippage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT NOT NULL,
                action TEXT,
                slippage REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def get_connection(self):
        return self._conn

    @contextmanager
    def transaction(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise


@pytest.fixture
def fake_db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(sg_module, 'db', fake)
    return fake


@pytest.fixture
def collected_alerts():
    """Recording notifier."""
    bag = []

    async def notifier(msg):
        bag.append(msg)

    return notifier, bag


# ============= is_pair_banned =============

def test_is_pair_banned_default_false(fake_db):
    g = SlippageGuard()
    assert g.is_pair_banned("BTCUSDT") is False


def test_is_pair_banned_true_for_active_ban(fake_db):
    """Если в settings лежит будущий timestamp — пара забанена."""
    future = (datetime.now(TIMEZONE) + timedelta(hours=10)).isoformat()
    fake_db.get_connection().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("ban_pair_BTCUSDT", future),
    )
    fake_db.get_connection().commit()

    g = SlippageGuard()
    assert g.is_pair_banned("BTCUSDT") is True


def test_is_pair_banned_false_for_expired_ban(fake_db):
    past = (datetime.now(TIMEZONE) - timedelta(hours=1)).isoformat()
    fake_db.get_connection().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        ("ban_pair_BTCUSDT", past),
    )
    fake_db.get_connection().commit()

    g = SlippageGuard()
    assert g.is_pair_banned("BTCUSDT") is False


# ============= record_event =============

def test_record_event_returns_count(fake_db):
    g = SlippageGuard()
    n1 = g.record_event("BTCUSDT", "OPEN", 0.025)
    n2 = g.record_event("BTCUSDT", "DCA", 0.03)
    assert n1 == 1
    assert n2 == 2


def test_record_event_per_pair(fake_db):
    """События одной пары не считаются для другой."""
    g = SlippageGuard()
    g.record_event("BTCUSDT", "OPEN", 0.02)
    g.record_event("BTCUSDT", "OPEN", 0.02)
    assert g.record_event("ETHUSDT", "OPEN", 0.02) == 1


def test_record_event_excludes_old(fake_db):
    """События старше 24ч в счёт не идут."""
    g = SlippageGuard()
    old_ts = (datetime.now(TIMEZONE) - timedelta(hours=25)).isoformat()
    fake_db.get_connection().execute(
        "INSERT INTO slippage_events (pair, action, slippage, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("BTCUSDT", "OPEN", 0.02, old_ts),
    )
    fake_db.get_connection().commit()

    n = g.record_event("BTCUSDT", "OPEN", 0.025)
    assert n == 1  # старое событие не учитывается


# ============= handle (record + maybe ban) =============

@pytest.mark.asyncio
async def test_handle_below_threshold_no_ban(fake_db, collected_alerts):
    notifier, bag = collected_alerts
    g = SlippageGuard(notifier=notifier, ban_threshold=3)
    banned = await g.handle("BTCUSDT", 0.02, "OPEN")
    assert banned is False
    assert g.is_pair_banned("BTCUSDT") is False
    assert bag == []


@pytest.mark.asyncio
async def test_handle_at_threshold_bans_and_alerts(fake_db, collected_alerts):
    notifier, bag = collected_alerts
    g = SlippageGuard(notifier=notifier, ban_threshold=3)

    await g.handle("BTCUSDT", 0.02, "OPEN")
    await g.handle("BTCUSDT", 0.025, "DCA")
    banned = await g.handle("BTCUSDT", 0.03, "OPEN")  # 3-е → ban

    assert banned is True
    assert g.is_pair_banned("BTCUSDT") is True
    assert len(bag) == 1
    assert "BTCUSDT" in bag[0] and "⛔️" in bag[0]


@pytest.mark.asyncio
async def test_handle_does_not_double_ban(fake_db, collected_alerts):
    notifier, bag = collected_alerts
    g = SlippageGuard(notifier=notifier, ban_threshold=2)

    await g.handle("BTCUSDT", 0.02, "OPEN")
    first = await g.handle("BTCUSDT", 0.025, "OPEN")  # 2-е → ban
    second = await g.handle("BTCUSDT", 0.03, "OPEN")  # 3-е → уже забанена

    assert first is True
    assert second is False  # повторного бана не было
    assert len(bag) == 1     # только один alert


@pytest.mark.asyncio
async def test_handle_without_notifier_still_bans(fake_db):
    """Если notifier=None — бан всё равно проставляется, просто без alert."""
    g = SlippageGuard(notifier=None, ban_threshold=2)
    await g.handle("BTCUSDT", 0.02, "OPEN")
    banned = await g.handle("BTCUSDT", 0.025, "OPEN")
    assert banned is True
    assert g.is_pair_banned("BTCUSDT") is True
