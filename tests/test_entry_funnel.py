"""Тесты воронки входа (чистая логика без сети)."""
import math

import pytest

from bot.services import entry_funnel as F
from bot.services.indicators_service import TechnicalIndicators as TI, closed_candles


# ----------------------------------------------------------------- helpers

def make_candles(closes, spread=0.004, volumes=None, start=0, step=3_600_000):
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) * (1 + spread / 2)
        lo = min(o, c) * (1 - spread / 2)
        out.append({'start': start + i * step, 'open': o, 'high': hi, 'low': lo, 'close': c,
                    'volume': (volumes[i] if volumes else 100.0)})
        prev = c
    return out


def uptrend(n, start=100.0, pct=0.003):
    return [start * (1 + pct) ** i for i in range(n)]


def ticker(symbol, price=10.0, spread=0.0002, turnover=20_000_000, change=0.03):
    half = price * spread / 2
    return {'symbol': symbol, 'lastPrice': str(price), 'bid1Price': str(price - half),
            'ask1Price': str(price + half), 'turnover24h': str(turnover), 'price24hPcnt': str(change)}


# ----------------------------------------------------------------- indicators

def test_closed_candles_drops_current_and_sorts():
    raw = [
        ['3000', '3', '4', '2', '3.5', '10', '0'],   # newest = в процессе
        ['2000', '2', '3', '1', '2.5', '10', '0'],
        ['1000', '1', '2', '0.5', '1.5', '10', '0'],
    ]
    out = closed_candles(raw)
    assert [c['start'] for c in out] == [1000, 2000]
    assert out[-1]['close'] == 2.5


def test_atr_series_last_matches_calculate_atr():
    c = make_candles(uptrend(60))
    h, l, cl = [x['high'] for x in c], [x['low'] for x in c], [x['close'] for x in c]
    series = TI.atr_series(h, l, cl, 14)
    assert series and math.isclose(series[-1], TI.calculate_atr(h, l, cl, 14), rel_tol=1e-3)


def test_ema_series_last_matches_calculate_ema():
    closes = uptrend(80)
    assert math.isclose(TI.ema_series(closes, 20)[-1], TI.calculate_ema(closes, 20), rel_tol=1e-6)


# ----------------------------------------------------------------- step 1

def test_universe_filters_liquidity_spread_change_and_perp():
    spot = [
        ticker('GOODUSDT'),
        ticker('THINUSDT', turnover=1_000_000),          # мало оборота
        ticker('WIDEUSDT', spread=0.005),                # широкий спред
        ticker('PUMPUSDT', change=0.25),                 # +25% за сутки
        ticker('NOPERPUSDT'),                            # нет перпа
        ticker('USDCUSDT'),                              # стейбл
        ticker('BTC3LUSDT'),                             # плечевой токен
    ]
    linear = [{'symbol': s, 'fundingRate': '0.0001'} for s in
              ['GOODUSDT', 'THINUSDT', 'WIDEUSDT', 'PUMPUSDT', 'USDCUSDT', 'BTC3LUSDT']]
    out = F.select_universe(spot, linear)
    assert [x['symbol'] for x in out] == ['GOODUSDT']
    assert out[0]['funding_rate'] == pytest.approx(0.0001)


def test_universe_respects_exclude():
    spot = [ticker('AUSDT'), ticker('BUSDT')]
    linear = [{'symbol': 'AUSDT'}, {'symbol': 'BUSDT'}]
    out = F.select_universe(spot, linear, exclude=['AUSDT'])
    assert [x['symbol'] for x in out] == ['BUSDT']


# ----------------------------------------------------------------- step 2

def test_trend_4h_up_and_down():
    ok, info = F.check_trend_4h(make_candles(uptrend(120)))
    assert ok and info['ema50_slope'] > 0
    ok, _ = F.check_trend_4h(make_candles(list(reversed(uptrend(120)))))
    assert not ok


def test_pullback_detected_after_dip_to_ema():
    closes = uptrend(150, pct=0.004)
    # откат последние 6 часов к EMA20
    top = closes[-1]
    closes += [top * (1 - 0.006 * i) for i in range(1, 6)] + [top * 0.975]
    ok, info = F.check_pullback_1h(make_candles(closes))
    assert 'atr_1h' in info and info['pullback_low'] < top
    assert ok, info.get('reason')


def test_no_pullback_in_vertical_rally():
    ok, info = F.check_pullback_1h(make_candles(uptrend(150, pct=0.01)))
    assert not ok  # RSI высоко и цена далеко от EMA


def test_relative_strength():
    coin = make_candles(uptrend(60, pct=0.004), step=14_400_000)
    btc = make_candles(uptrend(60, pct=0.001), step=14_400_000)
    ok, info = F.relative_strength(coin, btc, coin_change_24h=3.0, btc_change_24h=1.0)
    assert ok and info['rs_7d'] > 0
    ok, _ = F.relative_strength(btc, coin, coin_change_24h=1.0, btc_change_24h=3.0)
    assert not ok


# ----------------------------------------------------------------- step 3

def test_trigger_break_high():
    closes = [100.0] * 30 + [99.5, 99.0, 98.8, 100.2]
    c = make_candles(closes, step=900_000)
    ok, info = F.check_trigger_15m(c, current_price=100.3)
    assert ok and 'break_high' in info['trigger']


def test_trigger_rejects_chasing():
    closes = [100.0] * 30 + [99.5, 99.0, 98.8, 100.2]
    c = make_candles(closes, step=900_000)
    ok, info = F.check_trigger_15m(c, current_price=102.0)
    assert not ok and 'ушла' in info['reason']


def test_no_trigger_in_steady_decline():
    closes = [100.0 * (1 - 0.002 * i) for i in range(40)]
    ok, _ = F.check_trigger_15m(make_candles(closes, step=900_000), current_price=closes[-1])
    assert not ok


def test_trigger_volume_spike():
    closes = [100.0] * 30 + [100.0, 100.05]
    vols = [100.0] * 31 + [400.0]
    c = make_candles(closes, step=900_000, volumes=vols, spread=0.02)
    ok, info = F.check_trigger_15m(c, current_price=100.05)
    assert ok and 'volume' in info['trigger']


# ----------------------------------------------------------------- step 4

def test_orderbook_imbalance_and_wall():
    bids = [[str(99.9 - i * 0.1), '10'] for i in range(10)]
    asks = [[str(100.1 + i * 0.1), '5'] for i in range(10)]
    m = F.orderbook_metrics(bids, asks, 100.0)
    assert m['book_imbalance'] == pytest.approx(2.0, rel=0.05)
    assert not m['ask_wall']

    asks[1] = ['100.2', '200']
    m = F.orderbook_metrics(bids, asks, 100.0)
    assert m['ask_wall']


def test_aggressor_share_window():
    now = 10_000_000
    trades = [
        {'time': str(now - 60_000), 'side': 'Buy', 'size': '3', 'price': '1'},
        {'time': str(now - 60_000), 'side': 'Sell', 'size': '1', 'price': '1'},
        {'time': str(now - 3_600_000), 'side': 'Sell', 'size': '100', 'price': '1'},  # вне окна
    ]
    assert F.aggressor_share(trades, now) == pytest.approx(0.75)
    assert F.aggressor_share([], now) is None


def test_oi_labels():
    c1h = make_candles(uptrend(10))
    rising = [{'timestamp': str(i), 'openInterest': str(1000 + i * 10)} for i in range(6)]
    falling = [{'timestamp': str(i), 'openInterest': str(1000 - i * 10)} for i in range(6)]
    assert F.oi_signal(rising, c1h)['oi_label'] == 'new_money'
    assert F.oi_signal(falling, c1h)['oi_label'] == 'short_covering'
    assert F.oi_signal([], c1h)['oi_label'] == 'n/a'


# ----------------------------------------------------------------- step 5

def full_metrics(**over):
    m = dict(trend_ok=True, pullback_ok=True, trigger_ok=True, rs_ok=True,
             book_imbalance=1.5, ask_wall=False, aggressor_share=0.6,
             oi_label='new_money', funding_rate=0.0001, spread=0.0003, long_ratio=0.5)
    m.update(over)
    return m


def test_perfect_candidate_scores_100():
    fs = F.score_candidate(full_metrics())
    assert fs.score == 100 and not fs.vetoes


def test_weights_sum_to_100():
    assert sum(F.WEIGHTS.values()) == 100


def test_funding_veto_and_penalties():
    fs = F.score_candidate(full_metrics(funding_rate=0.001))
    assert fs.vetoes and 'funding' in fs.vetoes[0]

    fs = F.score_candidate(full_metrics(ask_wall=True, long_ratio=0.8, oi_label='short_covering'))
    assert fs.score < 80
    assert any('стена' in n for n in fs.notes)


def test_minimal_setup_below_threshold():
    # только тренд + триггер: 40 баллов — недостаточно
    fs = F.score_candidate(dict(trend_ok=True, trigger_ok=True))
    assert fs.score == 40


def test_trade_plan_stop_below_pullback_and_tp_in_r():
    plan, why = F.build_trade_plan(entry=100.0, pullback_low=98.5, atr_1h=1.0)
    assert plan is not None, why
    assert plan.stop == pytest.approx(98.4)
    assert plan.take_profit == pytest.approx(100 + 2.5 * 1.6)


def test_trade_plan_min_and_max_stop():
    plan, _ = F.build_trade_plan(entry=100.0, pullback_low=99.9, atr_1h=3.0)
    assert plan.entry - plan.stop == pytest.approx(1.5)   # не ближе 0.5×ATR
    plan, why = F.build_trade_plan(entry=100.0, pullback_low=96.0, atr_1h=1.0)
    assert plan is None and 'далеко' in why               # дальше 2.5×ATR — пропуск


def test_returns_correlation():
    a = [100 * (1 + 0.01 * math.sin(i)) for i in range(100)]
    b = [x * 2 for x in a]
    assert F.returns_correlation(a, b) == pytest.approx(1.0, abs=1e-6)
    assert F.returns_correlation(a[:10], b[:10]) is None


def test_trade_plan_rejects_fee_dominated_stop():
    # стоп 0.4% от цены: комиссии ≈0.25% — больше четверти риска
    plan, why = F.build_trade_plan(entry=100.0, pullback_low=99.7, atr_1h=0.5)
    assert plan is None and 'комиссии' in why


def test_trade_plan_has_partial_level_and_net_rr():
    plan, why = F.build_trade_plan(entry=100.0, pullback_low=98.5, atr_1h=1.0)
    assert plan.partial == pytest.approx(100 + 1.5 * 1.6)
    assert plan.net_rr == pytest.approx((4.0 - 0.25) / (1.6 + 0.25))
    assert plan.atr == 1.0
