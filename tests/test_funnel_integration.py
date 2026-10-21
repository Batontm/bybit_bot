"""
Сквозной прогон воронки и входа на фейковом клиенте Bybit (без сети):
scan() находит кандидата → open_position_with_plan() ставит post-only лимитку,
дожидается исполнения и записывает позицию по фактической цене.
"""
import time

import pytest

from bot.services.entry_funnel_service import EntryFunnelService
from bot.services import trading_service as ts_module
from bot.services.trading_service import TradingService


def _rows(closes, step_ms, spread=0.004, volumes=None):
    now = int(time.time() * 1000)
    start = now - step_ms * len(closes)
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        hi, lo = max(prev, c) * (1 + spread / 2), min(prev, c) * (1 - spread / 2)
        v = volumes[i] if volumes else 100
        rows.append([str(start + i * step_ms), str(prev), str(hi), str(lo), str(c), str(v), '0'])
        prev = c
    rows.append([str(now), str(prev), str(prev), str(prev), str(prev), '1', '0'])  # незакрытая
    return list(reversed(rows))


def _series():
    c4h = [50 * 1.006 ** i for i in range(130)]
    c1h = [60 * 1.004 ** i for i in range(200)]
    top = c1h[-1]
    c1h += [top * (1 - 0.006 * i) for i in range(1, 6)] + [top * 0.975]
    base = c1h[-1]
    c15 = [base] * 30 + [base * 0.995, base * 0.99, base * 0.988, base * 1.002]
    return c4h, c1h, c15


class FakeMarket:
    def __init__(self):
        self.c4h, self.c1h, self.c15 = _series()
        self.price = self.c15[-1]
        self.btc4h = [100 * 1.001 ** i for i in range(130)]

    def _ok(self, result):
        return {'retCode': 0, 'result': result}

    def get_tickers(self, category, symbol=None):
        p = self.price
        spot = [
            {'symbol': 'XUSDT', 'lastPrice': str(p), 'bid1Price': str(p * 0.9999), 'ask1Price': str(p * 1.0001),
             'turnover24h': '50000000', 'price24hPcnt': '0.04'},
            {'symbol': 'BTCUSDT', 'lastPrice': '100', 'bid1Price': '99.99', 'ask1Price': '100.01',
             'turnover24h': '900000000', 'price24hPcnt': '0.01'},
        ]
        if category == 'linear':
            return self._ok({'list': [{'symbol': 'XUSDT', 'fundingRate': '0.0001'},
                                      {'symbol': 'BTCUSDT', 'fundingRate': '0.0001'}]})
        if symbol:
            spot = [t for t in spot if t['symbol'] == symbol]
        return self._ok({'list': spot})

    def get_kline(self, category, symbol, interval, limit):
        if symbol == 'BTCUSDT':
            return self._ok({'list': _rows(self.btc4h, 14_400_000)})
        data = {'240': (self.c4h, 14_400_000), '60': (self.c1h, 3_600_000), '15': (self.c15, 900_000)}
        closes, step = data[interval]
        # 1h свечи пошире: стоп за откатом должен быть дальше комиссий (фильтр ≤25% риска)
        return self._ok({'list': _rows(closes, step, spread=0.012 if interval == '60' else 0.004)})

    def get_orderbook(self, category, symbol, limit):
        p = self.price
        return self._ok({'b': [[str(p * (1 - 0.0005 * i)), '20'] for i in range(1, 15)],
                         'a': [[str(p * (1 + 0.0005 * i)), '10'] for i in range(1, 15)]})

    def get_public_trade_history(self, category, symbol, limit):
        now = int(time.time() * 1000)
        return self._ok({'list': [{'time': str(now - 1000), 'side': 'Buy', 'size': '7', 'price': str(self.price)},
                                  {'time': str(now - 2000), 'side': 'Sell', 'size': '3', 'price': str(self.price)}]})

    def get_open_interest(self, category, symbol, intervalTime, limit):
        now = int(time.time() * 1000)
        return self._ok({'list': [{'timestamp': str(now - i * 3_600_000), 'openInterest': str(1000 - i * 20)}
                                  for i in range(limit)]})

    def get_long_short_ratio(self, category, symbol, period, limit):
        return self._ok({'list': [{'buyRatio': '0.5', 'sellRatio': '0.5'}]})


def test_scan_finds_candidate_with_plan():
    svc = EntryFunnelService(client=FakeMarket())
    cands = svc.scan()
    assert len(cands) == 1, 'ожидали одного кандидата XUSDT'
    c = cands[0]
    assert c['pair'] == 'XUSDT' and c['score'] >= 70
    assert c['plan'].stop < c['plan'].entry < c['plan'].take_profit
    assert c['signal_id']


def test_scan_skips_when_funding_overheated():
    market = FakeMarket()
    orig = market.get_tickers

    def hot(category, symbol=None):
        r = orig(category, symbol)
        if category == 'linear':
            for t in r['result']['list']:
                t['fundingRate'] = '0.002'
        return r
    market.get_tickers = hot
    assert EntryFunnelService(client=market).scan() == []


class FakeTradingClient(FakeMarket):
    def __init__(self, fill_status='Filled'):
        super().__init__()
        self.orders = []
        self.fill_status = fill_status
        self.cancelled = []
        self.prefix = str(time.time_ns())

    def place_order(self, **kw):
        oid = f"{self.prefix}-o{len(self.orders) + 1}"
        self.orders.append((oid, kw))
        return {'retCode': 0, 'result': {'orderId': oid}}

    def get_open_orders(self, category, symbol, orderId):
        kw = dict(self.orders)[orderId]
        if kw.get('side') == 'Buy':
            filled = self.fill_status == 'Filled' or orderId in self.cancelled
            status = self.fill_status if orderId not in self.cancelled else 'Cancelled'
            return {'retCode': 0, 'result': {'list': [{
                'orderStatus': status,
                'cumExecQty': kw['qty'] if self.fill_status == 'Filled' else '0',
                'avgPrice': kw.get('price', str(self.price)) if self.fill_status == 'Filled' else '0',
                'cumExecFee': str(float(kw['qty']) * 0.001) if self.fill_status == 'Filled' else '0',
            }]}}
        return {'retCode': 0, 'result': {'list': [{'orderStatus': 'New'}]}}

    get_order_history = get_open_orders

    def cancel_order(self, category, symbol, orderId):
        self.cancelled.append(orderId)
        return {'retCode': 0}

    def get_instruments_info(self, category, symbol):
        return {'retCode': 0, 'result': {'list': [{'lotSizeFilter': {'basePrecision': '0.0001'},
                                                   'priceFilter': {'tickSize': '0.0001'}}]}}


def _trading(monkeypatch, client):
    svc = TradingService.__new__(TradingService)
    svc.client = client
    from bot.db.trades_repo import TradesRepository
    svc.trades_repo = TradesRepository()
    monkeypatch.setattr(svc, 'check_risk_limits', lambda: (True, 'ok'))
    monkeypatch.setattr(svc, 'can_open_position_for_pair', lambda p: (True, 'OK'))
    monkeypatch.setattr(ts_module.balance_service, 'get_deposit_usdt', lambda: 10_000.0)
    monkeypatch.setattr(ts_module.balance_service, 'get_usdt_balance', lambda: {'free': 10_000.0})
    from bot.services.instrument_info import InstrumentInfo
    monkeypatch.setattr(ts_module, 'instrument_info', InstrumentInfo(client=client))
    monkeypatch.setattr(ts_module, 'FUNNEL_LIMIT_TIMEOUT_SEC', 1)
    # стоп ставит position_service — тем же фейковым клиентом
    from bot.services import position_service as psm
    from bot.services.position_service import PositionService
    ps = PositionService.__new__(PositionService)
    ps.client, ps.trades_repo = client, svc.trades_repo
    monkeypatch.setattr(psm, 'position_service', ps)
    monkeypatch.setattr(psm, 'instrument_info', InstrumentInfo(client=client))
    return svc


async def test_open_with_plan_limit_post_only(monkeypatch):
    client = FakeTradingClient()
    svc = _trading(monkeypatch, client)
    plan = EntryFunnelService(client=FakeMarket()).scan()[0]['plan']

    pos, err = await svc.open_position_with_plan('XUSDT', plan)
    assert err is None, err
    buy = client.orders[0][1]
    assert buy['orderType'] == 'Limit' and buy['timeInForce'] == 'PostOnly'
    # риск ≈ 0.5% депозита
    from bot.services.exit_rules import cost_per_unit
    risk = (pos['entry_price'] - pos['sl_price'] + cost_per_unit(pos['entry_price'])) * pos['quantity']
    assert risk == pytest.approx(50.0, rel=0.02)          # 0.5% капитала ВКЛЮЧАЯ комиссии
    # на бирже только стоп (StopOrder); тейки TP1/TP2 записаны в позицию и их ведёт бот
    assert not [kw for _, kw in client.orders if kw['side'] == 'Sell' and kw['orderType'] == 'Limit']
    assert pos['tp1_price'] and pos['tp1_price'] < pos['tp_price']
    # количество за вычетом комиссии в монете, TP/SL поставлены
    assert pos['quantity'] < float(buy['qty'])
    sells = [kw for _, kw in client.orders if kw['side'] == 'Sell']
    assert len(sells) == 1 and sells[0]['orderFilter'] == 'StopOrder'   # только стоп


async def test_open_with_plan_not_filled_cancels(monkeypatch):
    client = FakeTradingClient(fill_status='New')
    svc = _trading(monkeypatch, client)
    plan = EntryFunnelService(client=FakeMarket()).scan()[0]['plan']

    pos, err = await svc.open_position_with_plan('XUSDT', plan)
    assert pos is None and 'не исполнился' in err
    assert client.cancelled == [client.orders[0][0]]
    assert all(kw['side'] == 'Buy' for _, kw in client.orders)  # никаких TP/SL


async def _run_controller(monkeypatch, strategy='FUNNEL', funnel_trading=True):
    from bot import controller as ctrl_module
    from bot.services.entry_funnel_service import entry_funnel_service

    client = FakeTradingClient()
    trading = _trading(monkeypatch, client)
    monkeypatch.setattr(trading.trades_repo, 'get_open_positions', lambda pair=None: [])
    monkeypatch.setattr(entry_funnel_service, 'client', FakeMarket())
    monkeypatch.setattr(entry_funnel_service, '_kline_cache', {})
    monkeypatch.setattr(ctrl_module.market_regime_service, 'is_trading_allowed', lambda: (True, 'ok'))
    monkeypatch.setattr(ctrl_module, 'ENTRY_STRATEGY', strategy)

    ctrl = ctrl_module.BotController()
    ctrl.trading_service = trading
    ctrl.fixed_pairs = []
    ctrl.funnel_trading_enabled = funnel_trading
    sent, analyzed = [], []

    async def fake_notify(msg):
        sent.append(msg)

    async def fake_analyze(pair, score=None):
        analyzed.append(pair)
        return None

    monkeypatch.setattr(ctrl, '_send_notification', fake_notify)
    monkeypatch.setattr(ctrl.analysis_service, 'analyze_pair', fake_analyze)
    monkeypatch.setattr(ctrl, '_is_pair_banned', lambda p: False)
    await ctrl._check_signals()
    return client, sent, analyzed


async def test_controller_funnel_opens_position_without_ai(monkeypatch):
    client, sent, analyzed = await _run_controller(monkeypatch)
    assert any(kw['side'] == 'Buy' for _, kw in client.orders)
    assert sent and 'Балл воронки' in sent[0]
    assert analyzed == []  # воронка не зовёт никакой внешний анализ


async def test_controller_funnel_signal_only_no_orders(monkeypatch):
    client, sent, analyzed = await _run_controller(monkeypatch, funnel_trading=False)
    assert client.orders == []                       # сделок нет
    assert sent and 'Сигнал воронки (без сделки)' in sent[0]
    assert 'стоп' in sent[0]


def test_funnel_trading_default_off_and_toggle(monkeypatch):
    from bot import controller as ctrl_module
    from config import trading_config as cfg
    assert cfg.FUNNEL_TRADING_ENABLED is False
    ctrl = ctrl_module.BotController()
    before = ctrl.is_funnel_trading_enabled()
    assert ctrl.toggle_funnel_trading() is (not before)
    ctrl.toggle_funnel_trading()


def test_no_ai_modules_left():
    import importlib
    for name in ('bot.llm_router', 'bot.llm.prompts', 'bot.perplexity_client', 'bot.db.llm_requests_repo'):
        try:
            importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        raise AssertionError(f'{name} всё ещё в проекте')


def test_controller_starts_active_without_ai(monkeypatch):
    from bot import controller as ctrl_module
    ctrl = ctrl_module.BotController()
    assert ctrl.mode == 'ACTIVE'
    assert not hasattr(ctrl_module, 'llm_router')
