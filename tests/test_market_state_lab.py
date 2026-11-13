"""Состояние рынка (4 режима) и лаборатория стратегий."""
import math

from bot.services import market_state as MS
from bot.backtest import lab as L

D1 = MS.D1


def _days(closes, rng=1.0):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({'start': i * D1, 'open': o, 'high': max(o, c) + rng, 'low': min(o, c) - rng,
                    'close': c, 'volume': 1.0})
    return out


def _trend(n=220, step=0.5, start=100.0):
    return [start + i * step for i in range(n)]


# ---------------------------------------------------------------- состояние рынка

def test_state_up_in_steady_uptrend():
    assert MS.state_series(_days(_trend()))[-1] == MS.UP


def test_state_down_in_steady_downtrend():
    assert MS.state_series(_days(_trend(step=-0.3, start=200.0)))[-1] == MS.DOWN


def test_state_range_in_flat_market():
    closes = [100 + 2 * math.sin(i / 3) for i in range(220)]
    assert MS.state_series(_days(closes))[-1] == MS.RANGE


def test_state_panic_on_volatility_spike():
    d = _days(_trend())
    for x in d[-3:]:                       # три дня с диапазоном в 10 раз шире обычного
        x['high'] += 15
        x['low'] -= 15
    assert MS.state_series(d)[-1] == MS.PANIC


def test_state_none_until_enough_history():
    s = MS.state_series(_days(_trend(n=60)))
    assert all(x is None for x in s)
    assert MS.current_state(_days(_trend(n=60)))['state'] is None


def test_adx_high_in_trend_low_in_chop():
    trend = MS.adx_series(_days(_trend()))[-1]
    chop = MS.adx_series(_days([100 + 2 * math.sin(i / 3) for i in range(220)]))[-1]
    assert trend > 40 and chop < trend / 2


def test_lookup_uses_only_closed_days():
    d = _days(_trend())
    lk = MS.StateLookup(d)
    assert lk.at(0) is None
    assert lk.at(d[-1]['start'] + D1) == MS.UP
    # внутри последнего дня видно только состояние на закрытие предыдущего
    assert lk.at(d[-1]['start'] + D1 - 1) == MS.state_series(d)[-2]


# ---------------------------------------------------------------- симуляция

def _run(spec_key, closes, rng=1.0):
    spec = next(s for s in L.SPECS if s.key == spec_key)
    c = _days(closes, rng)
    return L.simulate(spec, 'XUSDT', c, L.indicators(c), D1, 0, len(c) * D1, lambda t: MS.UP)


def test_trend_long_wins_on_long_uptrend_and_trails():
    closes = [100 + (0.2 if i % 2 else -0.2) for i in range(130)] + [100 + i * 1.5 for i in range(1, 60)] \
        + [188 - i * 4 for i in range(1, 15)]
    trades = _run('trend_long', closes)
    assert trades and trades[0].side == 1
    assert trades[0].r > 2 and trades[0].exit == 'trail'


def test_trend_short_wins_on_long_downtrend():
    closes = [200 + (0.2 if i % 2 else -0.2) for i in range(130)] + [200 - i * 1.5 for i in range(1, 60)] \
        + [111 + i * 4 for i in range(1, 15)]
    trades = _run('trend_short', closes)
    assert trades and trades[0].side == -1
    assert trades[0].r > 2


def test_short_loses_about_1r_when_price_reverses_up():
    closes = [200 + (0.2 if i % 2 else -0.2) for i in range(130)] + [198, 196, 194, 200, 206, 212, 218]
    trades = _run('trend_short', closes)
    assert trades and -1.6 < trades[0].r < -0.5 and trades[0].exit in ('stop', 'trail')


def test_no_entry_outside_test_window():
    spec = next(s for s in L.SPECS if s.key == 'trend_long')
    c = _days([100 + (0.2 if i % 2 else -0.2) for i in range(130)] + [100 + i * 1.5 for i in range(1, 60)])
    assert L.simulate(spec, 'X', c, L.indicators(c), D1, 10_000 * D1, 20_000 * D1, lambda t: None) == []


def test_pairs_profit_when_spread_returns():
    n = 120
    a = [100.0] * n
    b = [100.0 + (0.3 if i % 2 else -0.3) for i in range(n)]
    for i in range(80, 84):
        a[i] = 100 + (i - 79) * 3          # A убежала вверх...
    for i in range(84, 92):
        a[i] = max(100.0, a[i - 1] - 2)    # ...и вернулась
    trades = L.simulate_pairs('AUSDT', 'BUSDT', _days(a, 0.1), _days(b, 0.1), 0, n * D1, lambda t: MS.RANGE)
    assert trades and trades[0].side == -1 and trades[0].exit == 'target' and trades[0].r > 0


# ---------------------------------------------------------------- допуск

def _t(r, opened):
    return L.LabTrade('s', 'X', 1, opened, opened + 1, r, 'stop', MS.UP)


def test_admission_needs_enough_trades():
    ok, why = L.admitted([_t(1.0, i) for i in range(10)], 5)
    assert not ok and 'мало' in why


def test_admission_rejects_one_lucky_half():
    trades = [_t(2.0, i) for i in range(15)] + [_t(-0.5, 100 + i) for i in range(15)]
    ok, why = L.admitted(trades, 50)
    assert not ok and 'нестабильно' in why


def test_admission_passes_when_positive_in_both_halves():
    trades = [_t(1.5 if i % 2 else -1.0, i) for i in range(20)] + [_t(1.5 if i % 2 else -1.0, 100 + i) for i in range(20)]
    ok, _ = L.admitted(trades, 50)
    assert ok


def test_report_renders():
    closes = [100 + (0.2 if i % 2 else -0.2) for i in range(160)] + [100 + i for i in range(1, 80)] \
        + [180 - i for i in range(1, 80)]
    h4 = []
    for d in _days(closes):
        for k in range(6):
            h4.append({**d, 'start': d['start'] + k * L.H4})
    data = {'BTCUSDT': {'240': h4}}
    res = L.run_lab(data, ['BTCUSDT'], 0, len(closes) * D1)
    text = "\n".join(L.report(res, ['BTCUSDT'], 0, len(closes) * D1))
    assert "стратегия × состояние рынка" in text and "Тренд: пробой вверх" in text
