"""Живые выходы в R (безубыток, трейлинг, время, TP1/TP2) и защита счёта."""
import time
from datetime import datetime, timedelta

import pytest

from config import trading_config as cfg
from config.settings import TIMEZONE


class _AmendClient:
    def __init__(self):
        self.amends = []
        self.orders = []

    def amend_order(self, **kw):
        self.amends.append(kw)
        return {'retCode': 0}

    def place_order(self, **kw):
        self.orders.append(kw)
        return {'retCode': 0, 'result': {'orderId': f'{time.time_ns()}'}}

    def cancel_order(self, **kw):
        return {'retCode': 0}


def _svc(client):
    from bot.services.position_service import PositionService
    from bot.db.trades_repo import TradesRepository
    from bot.db.pnl_repo import PnLRepository
    s = PositionService.__new__(PositionService)
    s.client = client
    s.trades_repo = TradesRepository()
    s.pnl_repo = PnLRepository()
    return s


def _open(repo, pair, entry, sl, qty=1.0, atr=None, hours_ago=0.0, price=None, **extra):
    opened = (datetime.now(TIMEZONE) - timedelta(hours=hours_ago)).isoformat()
    pid = repo.create_position({'pair': pair, 'entry_price': entry, 'quantity': qty, 'sl_price': sl,
                                'tp_price': entry + 2.5 * (entry - sl), 'opened_at': opened,
                                'initial_sl': sl, 'entry_atr': atr})
    if price is not None:
        repo.update_position_price(pid, price)
    if extra:
        repo.set_position_fields(pid, **extra)
    oid = f'sl{time.time_ns()}'
    repo.create_order({'order_id': oid, 'pair': pair, 'side': 'Sell', 'order_type': 'Market', 'price': sl,
                       'quantity': qty, 'status': 'Untriggered', 'is_sl': True, 'position_id': pid,
                       'created_at': opened, 'updated_at': opened})
    return pid


def _close_all(repo):
    for p in repo.get_open_positions():
        repo.close_position(p['id'], p.get('current_price') or p['entry_price'], 0)


async def test_breakeven_at_one_r_with_fee_buffer():
    c = _AmendClient()
    s = _svc(c)
    _close_all(s.trades_repo)
    pid = _open(s.trades_repo, 'BEUSDT', 100.0, 98.0, price=101.5)       # +0.75R — рано
    assert await s.check_breakeven() == []
    s.trades_repo.update_position_price(pid, 102.1)                        # +1.05R
    msgs = await s.check_breakeven()
    pos = s.trades_repo.get_position_by_id(pid)
    assert msgs and pos['sl_price'] == pytest.approx(100 * (1 + cfg.EXIT_BREAKEVEN_FEE_BUFFER))
    assert c.amends[-1]['triggerPrice']                                     # стоп изменён на бирже
    assert await s.check_breakeven() == []                                  # повторно не двигаем
    _close_all(s.trades_repo)


async def test_trailing_only_after_tp1_and_only_up():
    c = _AmendClient()
    s = _svc(c)
    _close_all(s.trades_repo)
    pid = _open(s.trades_repo, 'TRUSDT', 100.0, 98.0, atr=1.0, price=106.0)
    await s.update_trailing_stops()
    assert c.amends == []                                                   # TP1 ещё не было
    s.trades_repo.set_position_fields(pid, partial_taken=1, sl_price=100.25)
    await s.update_trailing_stops()
    assert s.trades_repo.get_position_by_id(pid)['sl_price'] == pytest.approx(104.0)   # 106 − 2×1
    s.trades_repo.update_position_price(pid, 105.0)
    await s.update_trailing_stops()
    assert s.trades_repo.get_position_by_id(pid)['sl_price'] == pytest.approx(104.0)   # вниз не двигаем
    _close_all(s.trades_repo)


async def test_time_exit_uses_r(monkeypatch):
    c = _AmendClient()
    s = _svc(c)
    _close_all(s.trades_repo)
    closed = []

    async def fake_close(pid, reason="manual"):
        closed.append((pid, reason))
        s.trades_repo.close_position(pid, 100.2, -1.0)
        return True
    monkeypatch.setattr(s, 'close_position', fake_close)

    stale = _open(s.trades_repo, 'OLDUSDT', 100.0, 98.0, hours_ago=cfg.EXIT_TIME_HOURS + 1, price=100.5)  # +0.25R
    moving = _open(s.trades_repo, 'GOUSDT', 100.0, 98.0, hours_ago=cfg.EXIT_TIME_HOURS + 1, price=101.5)  # +0.75R
    young = _open(s.trades_repo, 'NEWUSDT', 100.0, 98.0, hours_ago=1, price=100.1)
    msgs = await s.check_time_exit()
    assert [p for p, _ in closed] == [stale]
    assert msgs and "Выход по времени OLDUSDT" in msgs[0]
    _close_all(s.trades_repo)


async def test_tp1_then_tp2_full_pnl():
    c = _AmendClient()
    s = _svc(c)
    _close_all(s.trades_repo)
    pid = _open(s.trades_repo, 'TPUSDT', 100.0, 98.0, qty=10.0)
    m1 = await s._handle_exit_fill({'position_id': pid, 'is_tp': True}, 'Filled', 5.0, 103.0, 0.5)
    assert m1.startswith('🎯 TP1') and 'безубыток' in m1
    assert c.amends[-1]['qty'] == '5'                                    # SL урезан до остатка
    m2 = await s._handle_exit_fill({'position_id': pid, 'is_tp': True}, 'Filled', 5.0, 105.0, 0.5)
    pos = s.trades_repo.get_position_by_id(pid)
    # (103−100)×5 − 0.5  +  (105−100)×5 − 0.5 = 14.5 + 24.5 = 39
    assert pos['status'] == 'CLOSED' and pos['realized_pnl'] == pytest.approx(39.0)
    assert m2.startswith('🎯 TP TPUSDT')


async def test_stop_after_tp1_labelled_as_protected():
    c = _AmendClient()
    s = _svc(c)
    _close_all(s.trades_repo)
    pid = _open(s.trades_repo, 'BEXUSDT', 100.0, 98.0, qty=10.0)
    await s._handle_exit_fill({'position_id': pid, 'is_tp': True}, 'Filled', 5.0, 103.0, 0.0)
    msg = await s._handle_exit_fill({'position_id': pid, 'is_sl': True}, 'Filled', 5.0, 100.25, 0.0)
    assert msg.startswith('🛡')
    assert s.trades_repo.get_position_by_id(pid)['realized_pnl'] > 0


def test_tp_orders_split(monkeypatch):
    from bot.services.trading_service import TradingService
    from bot.services import trading_service as ts
    from bot.services.instrument_info import InstrumentInfo
    from bot.db.trades_repo import TradesRepository

    class IC:
        def get_instruments_info(self, **kw):
            return {'retCode': 0, 'result': {'list': [{'lotSizeFilter': {'basePrecision': '0.001'},
                                                       'priceFilter': {'tickSize': '0.01'}}]}}
    monkeypatch.setattr(ts, 'instrument_info', InstrumentInfo(client=IC()))
    t = TradingService.__new__(TradingService)
    t.client = _AmendClient()
    t.trades_repo = TradesRepository()
    import asyncio
    asyncio.run(t._place_tp_sl_orders('SPLITUSDT', 1.0, 105.0, 98.0, 1, tp1_price=103.0))
    limits = [o for o in t.client.orders if o['orderType'] == 'Limit']
    assert [(o['price'], o['qty']) for o in limits] == [('103', '0.5'), ('105', '0.5')]
    sl = [o for o in t.client.orders if o.get('triggerPrice')][0]
    assert sl['qty'] == '1'


# ----------------------------------------------------------------- защита счёта

def _closed_trade(repo, pnl, minutes_ago):
    pid = repo.create_position({'pair': 'GUARDUSDT', 'entry_price': 100.0, 'quantity': 1.0})
    repo.close_position(pid, 100.0, pnl)
    from bot.db.connection import get_transaction
    ts = (datetime.now(TIMEZONE) - timedelta(minutes=minutes_ago)).isoformat()
    with get_transaction() as conn:
        conn.execute("UPDATE positions SET closed_at = ?, net_pnl = ? WHERE id = ?", (ts, pnl, pid))


def test_loss_streak_pauses_entries_then_ack():
    from bot.services.account_guard import AccountGuard
    from bot.db.trades_repo import TradesRepository
    repo = TradesRepository()
    g = AccountGuard()
    # время «в будущем» на минуты — чтобы сделки других тестов в общей БД не попали в серию
    _closed_trade(repo, 5.0, -1)
    for m in (-2, -3, -4):
        _closed_trade(repo, -3.0, m)
    ok, why = g.entries_allowed()
    assert not ok and '3 убытков подряд' in why
    g.ack_loss_streak()
    assert g.entries_allowed()[0]
    _closed_trade(repo, 2.0, -5)                    # прибыльная сделка обнуляет серию
    assert g.loss_streak()[0] == 0


def test_drawdown_halt_and_reset():
    from bot.services.account_guard import AccountGuard
    g = AccountGuard()
    g.reset_drawdown(10_000)
    assert g.update_equity(10_500) is None          # новый пик
    assert g.update_equity(9_600) is None           # −8.6% — ещё можно
    msg = g.update_equity(9_400)                    # −10.5% — стоп
    assert msg and msg.startswith('⛔️')
    assert not g.entries_allowed()[0]
    assert g.update_equity(9_000) is None           # повторно не шлём
    g.reset_drawdown(9_000)
    assert g.entries_allowed()[0]


def test_risk_limits_respect_guard(monkeypatch):
    from bot.services.trading_service import TradingService
    from bot.services import account_guard as ag
    monkeypatch.setattr(ag.account_guard, 'entries_allowed', lambda: (False, 'стоп по просадке'))
    t = TradingService.__new__(TradingService)
    assert t.check_risk_limits() == (False, 'стоп по просадке')


def test_vol_scaling(monkeypatch):
    from bot.services.trading_service import TradingService
    from bot.services import market_regime_service as mrs
    from types import SimpleNamespace
    t = TradingService.__new__(TradingService)
    monkeypatch.setattr(mrs.market_regime_service, 'get_status', lambda **kw: SimpleNamespace(atr_ratio=1.6))
    frac, note = t.risk_fraction()
    assert frac == pytest.approx(cfg.MAX_RISK_PER_TRADE / 1.6) and 'волатилен' in note
    monkeypatch.setattr(mrs.market_regime_service, 'get_status', lambda **kw: SimpleNamespace(atr_ratio=5.0))
    assert t.risk_fraction()[0] == pytest.approx(cfg.MAX_RISK_PER_TRADE * cfg.VOL_RISK_MIN_FACTOR)
    monkeypatch.setattr(mrs.market_regime_service, 'get_status', lambda **kw: SimpleNamespace(atr_ratio=0.8))
    assert t.risk_fraction()[0] == cfg.MAX_RISK_PER_TRADE
