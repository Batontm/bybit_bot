"""v2.6: стоп как StopOrder, тейки ведёт бот, безопасный авто-SL, тренд, результаты, реальный счёт."""
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config import trading_config as cfg
from config.settings import TIMEZONE


class FakeClient:
    """Биржа: фиксирует ордера; рыночные продажи исполняются по заданной цене."""

    def __init__(self, sell_price=103.0, place_error=None, coin_balance=10.0):
        self.orders, self.cancels, self.amends = [], [], []
        self.sell_price = sell_price
        self.place_error = place_error
        self.coin_balance = coin_balance
        self._last = {}

    def place_order(self, **kw):
        self.orders.append(kw)
        if self.place_error and kw.get('triggerPrice'):
            err = self.place_error
            self.place_error = None if err == 'once' else err
            return {'retCode': 170131, 'retMsg': 'Insufficient balance.'}
        oid = f"o{time.time_ns()}"
        self._last[oid] = kw
        return {'retCode': 0, 'result': {'orderId': oid}}

    def get_order_history(self, category, symbol, orderId, **kw):
        kw_ = self._last.get(orderId, {})
        return {'retCode': 0, 'result': {'list': [{'orderStatus': 'Filled', 'cumExecQty': kw_.get('qty', '0'),
                                                   'avgPrice': str(self.sell_price), 'cumExecFee': '0.1'}]}}

    def cancel_order(self, **kw):
        self.cancels.append(kw)
        return {'retCode': 0}

    def amend_order(self, **kw):
        self.amends.append(kw)
        return {'retCode': 0}

    def get_wallet_balance(self, **kw):
        coin = kw.get('coin', 'X')
        return {'retCode': 0, 'result': {'list': [{'coin': [{'coin': coin, 'walletBalance': str(self.coin_balance)}]}]}}


def _ps(client):
    from bot.services.position_service import PositionService
    from bot.db.trades_repo import TradesRepository
    from bot.db.pnl_repo import PnLRepository
    s = PositionService.__new__(PositionService)
    s.client, s.trades_repo, s.pnl_repo = client, TradesRepository(), PnLRepository()
    return s


def _clear(repo):
    for p in repo.get_open_positions():
        repo.close_position(p['id'], p['entry_price'], 0)


def _pos(repo, pair='TPXUSDT', entry=100.0, sl=98.0, qty=10.0, tp1=103.0, tp=105.0, price=None, strategy='FUNNEL'):
    pid = repo.create_position({'pair': pair, 'entry_price': entry, 'quantity': qty, 'sl_price': sl,
                                'tp_price': tp, 'tp1_price': tp1, 'initial_sl': sl, 'strategy': strategy,
                                'risk_usd': qty * (entry - sl)})
    if price:
        repo.update_position_price(pid, price)
    return pid


# ----------------------------------------------------------------- стоп и тейки

async def test_stop_order_uses_stoporder_filter():
    c = FakeClient()
    s = _ps(c)
    _clear(s.trades_repo)
    pid = _pos(s.trades_repo)
    ok, oid = await s.place_stop_order('TPXUSDT', 10, 98.0, pid)
    assert ok and c.orders[-1]['orderFilter'] == 'StopOrder' and c.orders[-1]['triggerPrice']
    assert s.trades_repo.get_sl_order(pid)['order_id'] == oid
    _clear(s.trades_repo)


async def test_entry_places_only_stop_not_limit_tps(monkeypatch):
    from bot.services.trading_service import TradingService
    from bot.services import position_service as psm
    c = FakeClient()
    monkeypatch.setattr(psm, 'position_service', _ps(c))
    t = TradingService.__new__(TradingService)
    t.client = c
    await t._place_tp_sl_orders('TPXUSDT', 10.0, 105.0, 98.0, 1, tp1_price=103.0)
    assert [o.get('orderType') for o in c.orders] == ['Market']        # только стоп
    assert not any(o.get('orderType') == 'Limit' for o in c.orders)


async def test_bot_managed_tp1_then_tp():
    c = FakeClient(sell_price=103.0)
    s = _ps(c)
    _clear(s.trades_repo)
    pid = _pos(s.trades_repo, price=102.0)
    await s.place_stop_order('TPXUSDT', 10, 98.0, pid)
    assert await s.check_take_profits() == []                         # ниже TP1
    s.trades_repo.update_position_price(pid, 103.2)
    msgs = await s.check_take_profits()
    pos = s.trades_repo.get_position_by_id(pid)
    assert msgs and msgs[0].startswith('🎯 TP1') and pos['quantity'] == pytest.approx(5.0)
    assert pos['partial_taken'] == 1 and c.amends                      # стоп → безубыток и объём 5
    sell = [o for o in c.orders if o.get('orderType') == 'Market' and not o.get('triggerPrice')][0]
    assert sell['qty'] == '5'
    c.sell_price = 105.1
    s.trades_repo.update_position_price(pid, 105.2)
    msgs = await s.check_take_profits()
    assert msgs and msgs[0].startswith('🎯 TP')
    assert s.trades_repo.get_position_by_id(pid)['status'] == 'CLOSED'


async def test_trend_positions_have_no_take_profit():
    c = FakeClient()
    s = _ps(c)
    _clear(s.trades_repo)
    _pos(s.trades_repo, pair='TRNDUSDT', tp1=None, tp=0, price=200.0, strategy='TREND')
    assert await s.check_take_profits() == []
    _clear(s.trades_repo)


# ----------------------------------------------------------------- авто-SL больше не «теряет» позиции

async def test_auto_sl_insufficient_balance_keeps_position(monkeypatch):
    c = FakeClient(place_error='always', coin_balance=10.0)
    s = _ps(c)
    monkeypatch.setattr(s, '_get_actual_balance', lambda pair: 10.0)
    _clear(s.trades_repo)
    pid = _pos(s.trades_repo, pair='KEEPUSDT', price=100.0)
    msgs = await s.auto_create_missing_sl()
    pos = s.trades_repo.get_position_by_id(pid)
    assert pos['status'] == 'OPEN'                                      # раньше закрывалась в БД с PnL 0
    assert any('не удалось поставить стоп' in m for m in msgs)
    _clear(s.trades_repo)


async def test_auto_sl_frees_old_limit_tp_and_retries(monkeypatch):
    c = FakeClient(place_error='once')
    s = _ps(c)
    monkeypatch.setattr(s, '_get_actual_balance', lambda pair: 10.0)
    _clear(s.trades_repo)
    pid = _pos(s.trades_repo, pair='OLDTPUSDT', price=100.0)
    s.trades_repo.create_order({'order_id': f'tp{time.time_ns()}', 'pair': 'OLDTPUSDT', 'side': 'Sell',
                                'order_type': 'Limit', 'price': 105, 'quantity': 10, 'status': 'New',
                                'is_tp': True, 'position_id': pid, 'created_at': 'x', 'updated_at': 'x'})
    msgs = await s.auto_create_missing_sl()
    assert c.cancels and any(m.startswith('✅ AUTO-SL') for m in msgs)
    _clear(s.trades_repo)


async def test_auto_sl_uses_position_qty_not_whole_wallet(monkeypatch):
    c = FakeClient()
    s = _ps(c)
    monkeypatch.setattr(s, '_get_actual_balance', lambda pair: 25.0)   # монеты другой стратегии тоже тут
    _clear(s.trades_repo)
    _pos(s.trades_repo, pair='QTYUSDT', qty=10.0, price=100.0)
    await s.auto_create_missing_sl()
    assert c.orders[-1]['qty'] == '10'
    _clear(s.trades_repo)


async def test_rebuild_protection(monkeypatch):
    c = FakeClient()
    s = _ps(c)
    monkeypatch.setattr(s, '_get_actual_balance', lambda pair: 10.0)
    _clear(s.trades_repo)
    pid = _pos(s.trades_repo, pair='REBUSDT', price=100.0)
    for flag in ('is_tp', 'is_sl'):
        s.trades_repo.create_order({'order_id': f'{flag}{time.time_ns()}', 'pair': 'REBUSDT', 'side': 'Sell',
                                    'order_type': 'Limit', 'price': 1, 'quantity': 10, 'status': 'New',
                                    flag: True, 'position_id': pid, 'created_at': 'x', 'updated_at': 'x'})
    msgs = await s.rebuild_protection()
    assert len(c.cancels) == 2 and c.orders[-1]['orderFilter'] == 'StopOrder'
    assert msgs and '✅' in msgs[0]
    _clear(s.trades_repo)


# ----------------------------------------------------------------- тренд (живой)

class _Trading:
    def __init__(self, repo):
        self.trades_repo = repo
        self.opened = []

    async def open_trend_position(self, pair, stop, atr):
        self.opened.append((pair, stop, atr))
        return {'entry_price': 120.0, 'sl_price': stop}, None


class _DailyClient:
    def __init__(self, closes):
        self.closes = closes

    def get_kline(self, **kw):
        D = 86_400_000
        rows = []
        for i, c in enumerate(self.closes + [self.closes[-1]]):    # последняя — незакрытая
            rows.append([str(i * D), str(c * 0.995), str(c * 1.01), str(c * 0.98), str(c), '1', '1'])
        return {'retCode': 0, 'result': {'list': list(reversed(rows))}}


async def test_trend_service_enters_on_breakout_and_trails(monkeypatch):
    from bot.services.trend_strategy import TrendService
    from bot.db.trades_repo import TradesRepository
    repo = TradesRepository()
    _clear(repo)
    closes = [100 * 1.003 ** i for i in range(150)] + [100 * 1.003 ** 150 * 1.05]
    monkeypatch.setattr(cfg, 'TREND_PAIRS', ['TRBUSDT'])
    svc = TrendService(client=_DailyClient(closes))
    tr = _Trading(repo)
    msgs = await svc.run(tr, None)
    assert tr.opened and tr.opened[0][0] == 'TRBUSDT' and msgs[0].startswith('🐢 Тренд: открыта')
    # без разрешения входов — не открывает
    tr2 = _Trading(repo)
    await svc.run(tr2, None, allow_entries=False)
    assert tr2.opened == []
    # открытая трендовая позиция → подтяжка стопа
    pid = _pos(repo, pair='TRBUSDT', entry=100.0, sl=90.0, tp1=None, tp=0, strategy='TREND')
    amended = []

    class PS:
        async def _amend_sl(self, position_id, pair, new_sl, new_qty=None):
            amended.append(new_sl)
            return True
    msgs = await svc.run(tr, PS())
    assert amended and amended[0] > 90.0 and any('стоп подтянут' in m for m in msgs)
    _clear(repo)


def test_trend_slots_separate_from_funnel(monkeypatch):
    from bot.services.trading_service import TradingService
    from bot.services import account_guard as ag
    from bot.services import trading_service as ts
    monkeypatch.setattr(ag.account_guard, 'entries_allowed', lambda: (True, ''))
    t = TradingService.__new__(TradingService)
    positions = [{'pair': f'P{i}USDT', 'strategy': 'TREND', 'entry_price': 100, 'sl_price': 99, 'quantity': 0.01}
                 for i in range(3)]
    t.trades_repo = SimpleNamespace(get_open_positions=lambda pair=None: positions)
    t.pnl_repo = SimpleNamespace(get_pnl_by_days=lambda days=1: [], get_current_day_pnl=lambda: 0)
    monkeypatch.setattr(ts.balance_service, 'get_deposit_usdt', lambda: 10_000.0)
    ok, why = t.check_risk_limits(strategy="TREND")
    assert not ok and 'трендовых' in why
    assert t.check_risk_limits(strategy="FUNNEL")[0]                    # воронке тренд не мешает


# ----------------------------------------------------------------- результаты и реальный счёт

def test_results_screen(monkeypatch):
    from bot.telegram_aiogram import handlers as H
    rows = ([{'pair': 'AUSDT', 'strategy': 'FUNNEL', 'net_pnl': 30.0, 'realized_pnl': 30.0, 'risk_usd': 50.0,
              'closed_at': '2026-10-01', 'opened_at': ''}] * 3 +
            [{'pair': 'BUSDT', 'strategy': 'TREND', 'net_pnl': -50.0, 'realized_pnl': -50.0, 'risk_usd': 50.0,
              'closed_at': '2026-10-02', 'opened_at': ''}])
    monkeypatch.setattr(H, '_closed_positions', lambda days=0: rows)
    monkeypatch.setattr(H, 'balance_service', SimpleNamespace(last_total_equity=10_040.0,
                                                              get_deposit_usdt=lambda: 10_040.0))
    t = H._format_results(0)
    from tests.test_telegram_screens import assert_tg_html
    assert_tg_html(t)
    assert "🧭 Воронка" in t and "🐢 Тренд" in t
    assert "+0.60R" in t and "-1.00R" in t
    assert "❌ сделок ≥ 50 (сейчас 4)" in t and "Пока рано" in t


def test_results_empty(monkeypatch):
    from bot.telegram_aiogram import handlers as H
    monkeypatch.setattr(H, '_closed_positions', lambda days=0: [])
    assert "Закрытых сделок пока нет" in H._format_results(30)


def test_capital_limit(monkeypatch):
    from bot.services.balance_service import BalanceService
    b = BalanceService.__new__(BalanceService)
    monkeypatch.setattr(b, 'get_usdt_balance', lambda: {'total': 150_000.0})
    monkeypatch.setattr(cfg, 'TRADING_CAPITAL_LIMIT_USDT', 10_000)
    assert b.get_deposit_usdt() == 10_000
    monkeypatch.setattr(cfg, 'TRADING_CAPITAL_LIMIT_USDT', 0)
    assert b.get_deposit_usdt() == 150_000


def test_live_account_requires_confirmation(monkeypatch):
    from bot.controller import BotController
    import config.api_config as api
    c = BotController()
    monkeypatch.setattr(api, 'BYBIT_DEMO', False)
    monkeypatch.setattr(api, 'BYBIT_TESTNET', False)
    monkeypatch.delenv('LIVE_TRADING_CONFIRMED', raising=False)
    assert c.live_trading_status() == (True, False)
    c.set_mode("ACTIVE")
    assert c.mode == "RISK_ONLY"
    monkeypatch.setenv('LIVE_TRADING_CONFIRMED', 'yes')
    c.set_mode("ACTIVE")
    assert c.mode == "ACTIVE"
    c._exchange_pool.shutdown(wait=False)


def test_error_lines_readable():
    from bot.telegram_aiogram import handlers as H
    items = [{'timestamp': '2026-10-21T13:26:00.123465+02:00', 'module': 'Bybit', 'error_type': 'SLError',
              'message': "Insufficient balance. (ErrCode: 170131) (ErrTime: 11:26:00).\nRequest → POST "
                         "https://api-demo.bybit.com/v5/order/create: {'category': 'spot', 'symbol': 'SOLUSDT', "
                         "'side': 'Sell', 'orderType': 'Market', 'qty': '3.21', 'triggerPrice': '141.2'}"}]
    line = H._format_error_lines(items)[0]
    assert line.startswith("<code>13:26:00</code>") and "стоп" in line
    assert "symbol=SOLUSDT" in line and "qty=3.21" in line and "triggerPrice=141.2" in line
