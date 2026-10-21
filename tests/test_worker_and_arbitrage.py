"""Фоновый поток биржи, пауза, авто-арбитраж."""
import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest


# ----------------------------------------------------------------- поток биржи

def _controller():
    from bot.controller import BotController
    return BotController()


async def test_run_exchange_uses_worker_thread_and_is_reentrant():
    c = _controller()
    c._main_loop = asyncio.get_running_loop()
    names = []

    async def inner():
        names.append(threading.current_thread().name)
        return "inner"

    async def outer():
        names.append(threading.current_thread().name)
        # вложенный вызов изнутри потока биржи не должен зависнуть
        return await c.run_exchange(inner)

    result = await asyncio.wait_for(c.run_exchange(outer), timeout=5)
    assert result == "inner"
    assert all(n.startswith("exchange") for n in names)
    c._exchange_pool.shutdown(wait=False)


async def test_main_loop_not_blocked_while_job_runs():
    c = _controller()
    c._main_loop = asyncio.get_running_loop()

    async def slow_job():
        time.sleep(0.6)          # синхронный «запрос к бирже»
        return "done"

    t0 = time.monotonic()
    task = asyncio.create_task(c.run_exchange(slow_job))
    await asyncio.sleep(0.05)    # основной цикл продолжает работать
    assert time.monotonic() - t0 < 0.3
    assert await task == "done"
    c._exchange_pool.shutdown(wait=False)


async def test_notifications_from_worker_reach_main_loop():
    c = _controller()
    c._main_loop = asyncio.get_running_loop()
    got = []

    async def sink(msg):
        got.append((msg, asyncio.get_running_loop() is c._main_loop))

    c.notifier.set_callback(sink)

    async def job():
        await c._send_notification("🎯 TP ETHUSDT")

    await c.run_exchange(job)
    assert got == [("🎯 TP ETHUSDT", True)]
    c._exchange_pool.shutdown(wait=False)


async def test_run_job_survives_errors_and_signals_job_runs_off_worker():
    c = _controller()
    c._main_loop = asyncio.get_running_loop()
    seen = {}

    async def boom():
        raise RuntimeError("bybit down")

    async def signals():
        seen['thread'] = threading.current_thread().name

    c._boom = boom
    c._check_signals = signals
    await c.run_job('_boom')                  # ошибка логируется, не падает
    await c.run_job('_check_signals')
    assert not seen['thread'].startswith("exchange")
    c._exchange_pool.shutdown(wait=False)


def test_scheduler_wraps_jobs_through_worker():
    from bot.services.job_scheduler import setup_jobs

    class Sched:
        def __init__(self):
            self.jobs = []

        def add_job(self, cb, **kw):
            self.jobs.append((cb, kw))

    c = _controller()
    s = Sched()
    setup_jobs(s, c)
    cb, kw = s.jobs[0]
    assert getattr(cb, 'func', None) == c.run_job and kw['coalesce'] and kw['misfire_grace_time'] == 60
    c._exchange_pool.shutdown(wait=False)


# ----------------------------------------------------------------- пауза

async def test_panic_pause_survives_midnight():
    c = _controller()
    c.set_mode("PAUSED", reason="panic")
    await c._reset_daily_limits()
    assert c.mode == "PAUSED"
    c.set_mode("ACTIVE")
    assert c.pause_reason == ""
    c._exchange_pool.shutdown(wait=False)


# ----------------------------------------------------------------- арбитраж

def test_funding_entry_requires_fee_payback():
    from bot.services.arbitrage_service import ArbitrageService as A
    assert A.MIN_FUNDING_RATE == pytest.approx(0.0031 / 9)
    assert not A.evaluate_funding('BTCUSDT', 0.0001)['entry_ok']     # окупится за ~10 дней
    ok = A.evaluate_funding('BTCUSDT', 0.0004)
    assert ok['entry_ok'] and ok['breakeven_days'] == pytest.approx(0.0031 / 0.0012)
    assert not A.evaluate_funding('SOLUSDT', 0.001)['entry_ok']      # не в whitelist
    assert not A.evaluate_funding('ETHUSDT', -0.0001)['entry_ok']


class _ArbClient:
    def __init__(self, history, coin_balance=0.0099):
        self.history = history
        self.coin_balance = coin_balance
        self.orders = []

    def get_funding_rate_history(self, **kw):
        return {'retCode': 0, 'result': {'list': [
            {'fundingRateTimestamp': str(ts), 'fundingRate': str(r)} for ts, r in self.history]}}

    def get_tickers(self, **kw):
        return {'retCode': 0, 'result': {'list': [{'markPrice': '60000', 'lastPrice': '60000', 'fundingRate': '-0.0001'}]}}

    def get_wallet_balance(self, **kw):
        return {'retCode': 0, 'result': {'list': [{'coin': [
            {'coin': 'BTC', 'walletBalance': str(self.coin_balance), 'locked': '0'}]}]}}

    def place_order(self, **kw):
        self.orders.append(kw)
        return {'retCode': 0, 'result': {'orderId': f'o{len(self.orders)}'}}

    def get_instruments_info(self, **kw):
        return {'retCode': 0, 'result': {'list': [{'lotSizeFilter': {'basePrecision': '0.000001', 'minOrderQty': '0.001',
                                                                     'qtyStep': '0.001'},
                                                   'priceFilter': {'tickSize': '0.01'}}]}}


def _arb_service(client):
    from bot.services.arbitrage_service import ArbitrageService
    from bot.db.arbitrage_repo import ArbitrageRepository
    svc = ArbitrageService.__new__(ArbitrageService)
    svc.client = client
    svc.repo = ArbitrageRepository()
    svc.repo.init_table()
    return svc


def _open_position(svc, opened_hours_ago=30):
    opened = datetime.now(timezone.utc) - timedelta(hours=opened_hours_ago)
    pid = svc.repo.create_position({'pair': 'BTCUSDT', 'spot_qty': 0.0099, 'futures_qty': 0.01,
                                    'entry_price': 60000.0})
    from bot.db.connection import get_transaction
    with get_transaction() as conn:
        conn.execute("UPDATE arbitrage_positions SET opened_at = ? WHERE id = ?", (opened.isoformat(), pid))
    return pid, opened


async def test_settle_funding_counts_once_and_exits_on_negative(monkeypatch):
    from bot.services import instrument_info as ii
    now_ms = int(time.time() * 1000)
    h = 3_600_000
    history = [(now_ms - 40 * h, 0.001),        # до открытия — не считается
               (now_ms - 24 * h, 0.0002),
               (now_ms - 16 * h, -0.0001),
               (now_ms - 8 * h, -0.0002)]
    client = _ArbClient(history)
    svc = _arb_service(client)
    monkeypatch.setattr(ii, 'instrument_info', ii.InstrumentInfo(client=client))
    pid, _ = _open_position(svc)

    messages = await svc.settle_funding()
    pos = svc.repo.get_position_by_id(pid)
    expected = (0.0002 - 0.0001 - 0.0002) * 0.01 * 60000
    assert pos['accumulated_funding'] == pytest.approx(expected)
    assert pos['funding_count'] == 3
    # два последних начисления отрицательные → позиция закрыта
    assert pos['status'] == 'CLOSED'
    assert messages and "funding < 0" in messages[0]
    # спот продан не больше реального остатка, шорт закрыт reduceOnly
    spot = [o for o in client.orders if o['category'] == 'spot'][0]
    fut = [o for o in client.orders if o['category'] == 'linear'][0]
    assert float(spot['qty']) <= 0.0099 and fut['reduceOnly'] is True


async def test_settle_funding_keeps_positive_position():
    now_ms = int(time.time() * 1000)
    h = 3_600_000
    client = _ArbClient([(now_ms - 16 * h, 0.0004), (now_ms - 8 * h, 0.0003)])
    svc = _arb_service(client)
    pid, _ = _open_position(svc)
    await svc.settle_funding()
    first = svc.repo.get_position_by_id(pid)
    await svc.settle_funding()                  # повторный учёт не удваивает
    second = svc.repo.get_position_by_id(pid)
    assert first['status'] == 'OPEN'
    assert second['accumulated_funding'] == pytest.approx(first['accumulated_funding'])
    assert first['accumulated_funding'] == pytest.approx(0.0007 * 0.01 * 60000)
    svc.repo.close_position(pid, 0)


async def test_close_failure_keeps_position_open():
    client = _ArbClient([])

    def failing(**kw):
        client.orders.append(kw)
        return {'retCode': 170131, 'retMsg': 'Insufficient balance'} if kw['category'] == 'spot' \
            else {'retCode': 0, 'result': {'orderId': 'x'}}
    client.place_order = failing
    svc = _arb_service(client)
    pid, _ = _open_position(svc)
    ok, msg = await svc.close_arbitrage(pid)
    assert not ok and "вручную" in msg
    assert svc.repo.get_position_by_id(pid)['status'] == 'OPEN'
    assert any(o['category'] == 'linear' for o in client.orders)   # вторая нога всё равно закрывалась
    svc.repo.close_position(pid, 0)
