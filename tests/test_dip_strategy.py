"""Покупка на просадке: индикаторы, сигнал входа, симуляция; исправления светофора и RSI."""
import math

from config import trading_config as cfg
from bot.backtest import engine as E
from bot.services import dip_strategy as D
from bot.services.indicators_service import TechnicalIndicators as TI

H4 = D.H4


def _candles(closes, spread=0.5):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({'start': i * H4, 'open': o, 'high': max(o, c) + spread, 'low': min(o, c) - spread,
                    'close': c, 'volume': 1.0})
    return out


def _flat_then_drop(n_flat=60, drop=(98, 95, 91, 86)):
    flat = [100 + (0.3 if i % 2 else -0.3) for i in range(n_flat)]
    return flat + list(drop)


# ---------------------------------------------------------------- RSI (Уайлдер)

def test_rsi_is_wilder_smoothed():
    closes = [100, 101, 100.5, 102, 101, 103, 102.5, 104, 103, 105, 104.5, 106, 105, 107, 106,
              104, 105, 103, 104, 102, 103, 101, 102, 100, 99, 100, 98, 97, 98, 96]
    period = 14
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = sum(max(d, 0) for d in deltas[:period]) / period
    l = sum(max(-d, 0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        g = (g * (period - 1) + max(d, 0)) / period
        l = (l * (period - 1) + max(-d, 0)) / period
    expected = 100 - 100 / (1 + g / l)
    assert math.isclose(TI.rsi_wilder(closes, period), expected, abs_tol=0.01)
    # и совпадает с рядом из dip_strategy
    assert math.isclose(D.rsi_series(closes, period)[-1], expected, abs_tol=1e-9)


def test_rsi_differs_from_simple_average_on_long_history():
    """Старый расчёт (простое среднее последних 14) игнорировал историю — новый её учитывает."""
    closes = [100 + 10 * math.sin(i / 7) + i * 0.05 for i in range(200)]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))][-14:]
    g, l = sum(max(d, 0) for d in deltas) / 14, sum(max(-d, 0) for d in deltas) / 14
    simple = 100 - 100 / (1 + g / l) if l else 100.0
    assert abs(TI.rsi_wilder(closes, 14) - simple) > 0.5
    assert math.isclose(TI.calculate_rsi(closes, 14), simple, abs_tol=0.01)   # воронка — прежний расчёт


# ---------------------------------------------------------------- светофор: прогрев EMA200

def test_regime_uses_wilder_rsi():
    from bot.services.market_regime_service import regime_metrics
    closes = [100 + 10 * math.sin(i / 7) + i * 0.05 for i in range(400)]
    candles = [{'close': c, 'high': c + 1, 'low': c - 1} for c in closes]
    _, _, rsi, _ = regime_metrics(candles)
    assert math.isclose(rsi, TI.rsi_wilder(closes, 14), abs_tol=0.01)


def test_regime_requests_long_history_for_ema200():
    from bot.services.market_regime_service import MarketRegimeService
    assert MarketRegimeService.KLINE_LIMIT >= 5 * MarketRegimeService.EMA_PERIOD


def test_ema200_short_history_is_biased_after_a_fall():
    """На 250 свечах EMA200 после падения завышена относительно полностью прогретой."""
    closes = [100.0] * 600 + [100 - i * 0.1 for i in range(1, 201)]
    short = TI.calculate_ema(closes[-250:], 200)
    full = TI.calculate_ema(closes, 200)
    assert short > full                          # завышена...
    assert (short - full) / full > 0.002         # ...заметно (>0.2%)


# ---------------------------------------------------------------- сигнал входа

def test_no_signal_in_flat_market():
    c = _candles([100 + (0.3 if i % 2 else -0.3) for i in range(80)])
    ok, info = D.entry_signal(c)
    assert not ok


def test_signal_after_sharp_drop_and_stop_below_entry():
    c = _candles(_flat_then_drop())
    ok, info = D.entry_signal(c)
    assert ok, info
    assert info['rsi'] <= cfg.DIP_RSI_MAX
    assert info['close'] < info['lower'] < info['mid']
    assert math.isclose(info['stop'], info['close'] - cfg.DIP_STOP_ATR * info['atr'])


def test_trend_filter_blocks_dip_below_long_ema():
    c = _candles(_flat_then_drop())
    with E.override({'DIP_TREND_EMA': 50}):
        ok, info = D.entry_signal(c)
    assert not ok and 'EMA50' in info['reason']


def test_green_candle_confirmation():
    c = _candles(_flat_then_drop())
    with E.override({'DIP_CONFIRM_GREEN': True}):
        ok, info = D.entry_signal(c)        # последняя свеча красная
    assert not ok and 'зелён' in info['reason']


def test_short_history_returns_false():
    assert D.entry_signal(_candles([100, 99, 98]))[0] is False


# ---------------------------------------------------------------- симуляция

def _run(closes, **kw):
    c = _candles(closes)
    data = {'BTCUSDT': {'240': c}}
    return E.run_dip(data, ['BTCUSDT'], start_ts=0, end_ts=len(c) * H4, equity=10_000.0, **kw)


def test_backtest_takes_profit_on_return_to_mean():
    closes = _flat_then_drop() + [88, 91, 94, 97, 99, 100, 100]
    res = _run(closes)
    assert res.trades
    t = res.trades[-1]
    assert t.exit_kind == 'target' and t.pnl > 0
    # риск первой сделки = DIP_RISK_PER_TRADE капитала (с учётом комиссий)
    assert math.isclose(res.trades[0].risk_usd, 10_000 * cfg.DIP_RISK_PER_TRADE, rel_tol=0.02)


def test_backtest_stops_out_when_fall_continues():
    closes = _flat_then_drop() + [80, 72, 65, 60]
    res = _run(closes)
    assert res.trades and res.trades[0].exit_kind == 'stop'
    assert res.trades[0].pnl < 0
    assert res.trades[0].r_multiple <= -0.9      # убыток ≈ 1R (гэп через стоп — хуже)


def test_backtest_time_exit():
    drop = _flat_then_drop()
    closes = drop + [drop[-1] - 0.2 + (0.1 if i % 2 else -0.1) for i in range(40)]
    with E.override({'DIP_MAX_BARS': 5, 'DIP_STOP_ATR': 20.0, 'DIP_MAX_POSITIONS': 1}):
        res = _run(closes)
    assert res.trades[0].exit_kind == 'time'


def test_dip_is_disabled_in_live_bot_by_default():
    assert cfg.DIP_ENABLED is False
