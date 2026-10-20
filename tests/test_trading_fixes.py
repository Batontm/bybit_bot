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
    assert cfg.STALE_MOVE_THRESHOLD > 0


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
