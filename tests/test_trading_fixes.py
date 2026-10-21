"""Регрессионные тесты исправлений v2.2: риск, докупки, точность, светофор."""
import pytest

from bot.services.instrument_info import InstrumentInfo, step_decimals
from bot.services.trading_service import TradingService
from bot.services import trading_service as ts_module
from bot.services.market_regime_service import MarketRegimeService
from config import trading_config as cfg


# ----------------------------------------------------------------- риск

def test_open_risk_uses_distance_to_stop():
    positions = [
        {'entry_price': 100.0, 'sl_price': 98.0, 'quantity': 5},   # риск 10
        {'entry_price': 50.0, 'sl_price': 51.0, 'quantity': 10},   # SL в плюсе — 0
    ]
    assert TradingService.open_risk_usdt(positions) == pytest.approx(10.0)


def test_open_risk_without_stop_uses_default():
    positions = [{'entry_price': 100.0, 'sl_price': None, 'quantity': 1}]
    assert TradingService.open_risk_usdt(positions) == pytest.approx(100 * cfg.DEFAULT_SL_PERCENT)


# ----------------------------------------------------------------- докупки

def _svc():
    return TradingService.__new__(TradingService)  # без сетевого клиента


def test_add_action_disabled_by_default():
    pos = {'entry_price': 100, 'current_price': 90, 'dca_count': 0}
    assert _svc().get_add_action(pos) is None


def test_pyramiding_trigger_requires_drawdown(monkeypatch):
    monkeypatch.setattr(ts_module, 'PYRAMIDING_ENABLED', True)
    monkeypatch.setattr(ts_module, 'PYRAMIDING_TRIGGER', -0.005)
    svc = _svc()
    # в прибыли — докупки быть не должно (раньше триггер +0.01 срабатывал сразу)
    assert svc.get_add_action({'entry_price': 100, 'current_price': 100.9, 'dca_count': 0}) is None
    assert svc.get_add_action({'entry_price': 100, 'current_price': 99.4, 'dca_count': 0}) == "PYRAMIDING"


def test_dca_after_pyramiding(monkeypatch):
    monkeypatch.setattr(ts_module, 'PYRAMIDING_ENABLED', True)
    monkeypatch.setattr(ts_module, 'DCA_ENABLED', True)
    svc = _svc()
    pos = {'entry_price': 100, 'avg_entry_price': 100, 'current_price': 98.0, 'dca_count': 1}
    assert svc.get_add_action(pos) == "DCA"
    pos['dca_count'] = cfg.DCA_MAX_ENTRIES
    assert svc.get_add_action(pos) is None


def test_config_signs():
    assert cfg.PYRAMIDING_TRIGGER < 0
    assert cfg.DCA_TRIGGER_PERCENT < 0
    assert cfg.EXIT_TIME_MIN_R > 0
    assert cfg.EXIT_BREAKEVEN_FEE_BUFFER > 2 * cfg.TRADING_FEE_RATE   # безубыток покрывает комиссии


# ----------------------------------------------------------------- точность

def test_step_decimals():
    assert step_decimals('0.0001') == 4
    assert step_decimals('1') == 0
    assert step_decimals('0.50') == 1


class _FakeClient:
    def get_instruments_info(self, category, symbol):
        return {'retCode': 0, 'result': {'list': [{
            'lotSizeFilter': {'basePrecision': '0.1'},
            'priceFilter': {'tickSize': '0.00001'},
        }]}}


def test_instrument_info_formats_cheap_coin():
    info = InstrumentInfo(client=_FakeClient())
    assert info.format_price('CHEAPUSDT', 0.1234567) == '0.12346'
    assert info.format_qty('CHEAPUSDT', 1234.56789) == '1234.5'   # вниз


# ----------------------------------------------------------------- светофор

def _kline_resp(n, spike=False):
    rows = []
    price = 100.0
    for i in range(n):
        rng = 0.5
        if spike and i >= n - 3:
            rng = 6.0
        price *= 1.002
        rows.append([str(i * 14_400_000), str(price), str(price + rng), str(price - rng), str(price), '1', '1'])
    rows.append([str(n * 14_400_000), str(price), str(price), str(price), str(price), '0', '0'])  # текущая
    return {'retCode': 0, 'result': {'list': list(reversed(rows))}}


class _RegimeClient:
    def __init__(self, spike):
        self.spike = spike

    def get_kline(self, **kw):
        return _kline_resp(260, spike=self.spike)


def test_regime_atr_spike_now_blocks():
    svc = MarketRegimeService.__new__(MarketRegimeService)
    svc._client = _RegimeClient(spike=True)
    from datetime import datetime
    st = svc._compute_status(datetime.now())
    assert st.atr_ratio is not None and st.atr_ratio > 2
    assert not st.allowed and 'ATR spike' in st.reason


def test_regime_calm_market_allowed():
    svc = MarketRegimeService.__new__(MarketRegimeService)
    svc._client = _RegimeClient(spike=False)
    from datetime import datetime
    st = svc._compute_status(datetime.now())
    assert st.atr_ratio == pytest.approx(1.0, abs=0.1)
    assert st.allowed, st.reason


# ----------------------------------------------------------------- технический анализ (без ИИ)

async def test_analysis_service_technical_signal(monkeypatch):
    from bot.services.analysis_service import AnalysisService
    svc = AnalysisService.__new__(AnalysisService)
    monkeypatch.setattr(svc, 'get_market_data', lambda pair: {'price': 1.0, 'rsi': 45, 'tech_score': 55})
    a = await svc.analyze_pair('XUSDT')
    assert a['score'] == 55 and a['signal'] == 'WAIT'
    a = await svc.analyze_pair('XUSDT', score=75)
    assert a['score'] == 75 and a['signal'] == 'BUY'
    a = await svc.analyze_pair('XUSDT', score=30)
    assert a['signal'] == 'AVOID'


# ----------------------------------------------------------------- закрытие по TP/SL

class _CancelClient:
    def __init__(self):
        self.cancelled = []

    def cancel_order(self, **kw):
        self.cancelled.append(kw)
        return {'retCode': 0}


async def test_tp_fill_closes_position_with_real_pnl():
    import time
    from bot.services.position_service import PositionService
    from bot.db.trades_repo import TradesRepository
    repo = TradesRepository()
    pid = repo.create_position({'pair': 'ETHUSDT', 'entry_price': 2000.0, 'quantity': 0.5,
                                'tp_price': 2100.0, 'sl_price': 1950.0, 'commission_paid': 1.0})
    now = '2026-10-21T10:00:00+02:00'
    uid = str(time.time_ns())
    tp = {'order_id': f'tp{uid}', 'pair': 'ETHUSDT', 'side': 'Sell', 'order_type': 'Limit', 'price': 2100.0,
          'quantity': 0.5, 'status': 'New', 'is_tp': True, 'position_id': pid, 'created_at': now, 'updated_at': now}
    sl = dict(tp, order_id=f'sl{uid}', order_type='Market', price=1950.0, is_tp=False, is_sl=True,
              status='Untriggered')
    repo.create_order(tp)
    repo.create_order(sl)

    svc = PositionService.__new__(PositionService)
    svc.trades_repo = repo
    svc.client = _CancelClient()
    from bot.db.pnl_repo import PnLRepository
    svc.pnl_repo = PnLRepository()

    msg = await svc._handle_exit_fill(dict(tp, position_id=pid), 'Filled', 0.5, 2100.0, 1.05)
    pos = repo.get_position_by_id(pid)
    assert pos['status'] == 'CLOSED'
    # (2100-2000)*0.5 = 50, минус комиссии 1.0 + 1.05
    assert pos['realized_pnl'] == pytest.approx(47.95)
    assert msg.startswith('🎯 TP ETHUSDT') and '+$47.95' in msg
    # второй защитный ордер (условный SL в статусе Untriggered) отменён
    assert any(c.get('orderId') == f'sl{uid}' for c in svc.client.cancelled)


async def test_partial_fill_reduces_quantity():
    import time
    from bot.services.position_service import PositionService
    from bot.db.trades_repo import TradesRepository
    repo = TradesRepository()
    pid = repo.create_position({'pair': 'SOLUSDT', 'entry_price': 100.0, 'quantity': 10.0})
    svc = PositionService.__new__(PositionService)
    svc.trades_repo = repo
    svc.client = _CancelClient()
    msg = await svc._handle_exit_fill({'position_id': pid, 'is_tp': True}, 'Filled', 4.0, 105.0, 0.0)
    # TP1: часть зафиксирована, позиция открыта с остатком, прибыль части учтена
    assert msg.startswith('🎯 TP1 SOLUSDT')
    pos = repo.get_position_by_id(pid)
    assert pos['status'] == 'OPEN' and pos['quantity'] == pytest.approx(6.0)
    assert pos['partial_taken'] == 1 and pos['partial_pnl'] == pytest.approx(20.0)
    # SL-ордера в БД нет → стоп отмечен в позиции и предупреждение в сообщении
    assert '⚠️' in msg and pos['sl_price'] == pytest.approx(100 * (1 + cfg.EXIT_BREAKEVEN_FEE_BUFFER))


def test_order_position_id_lookup():
    import time
    from bot.db.trades_repo import TradesRepository
    repo = TradesRepository()
    oid = f'o{time.time_ns()}'
    repo.create_order({'order_id': oid, 'pair': 'X', 'side': 'Buy', 'order_type': 'Market', 'quantity': 1,
                       'status': 'Filled', 'position_id': 42, 'created_at': 'x', 'updated_at': 'x'})
    assert repo.get_order_position_id(oid) == 42


def test_balance_free_without_deprecated_field(monkeypatch):
    from bot.services.balance_service import BalanceService
    svc = BalanceService.__new__(BalanceService)
    svc.last_total_equity = None

    class C:
        def get_wallet_balance(self, **kw):
            return {'retCode': 0, 'result': {'list': [{'totalEquity': '1500.5', 'coin': [
                {'coin': 'USDT', 'walletBalance': '1000', 'availableToWithdraw': '', 'locked': '100',
                 'totalOrderIM': '', 'usdValue': '1000'},
                {'coin': 'ETH', 'walletBalance': '0.2', 'availableToWithdraw': '', 'locked': '0', 'usdValue': '500.5'},
            ]}]}}
    svc.client = C()
    b = svc.get_wallet_balance()
    assert b['USDT']['free'] == pytest.approx(900.0)      # раньше было 0
    assert b['ETH']['usd_value'] == pytest.approx(500.5)
    assert svc.last_total_equity == pytest.approx(1500.5)
