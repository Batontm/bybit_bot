"""
Состояние рынка — четыре режима вместо двух у светофора.

Считается по ДНЕВНЫМ закрытым свечам BTC (рынок альткоинов идёт за ним):
  PANIC — резкий скачок волатильности: средний дневной диапазон за 3 дня
          в STATE_PANIC_RATIO раз больше среднего за 30 дней;
  RANGE — боковик: ADX(14) ниже STATE_ADX_MIN, либо цена и средние «не договорились»
          (переход между трендами);
  UP    — рост: ADX не ниже порога, цена выше EMA100 и EMA20 выше EMA100;
  DOWN  — падение: ADX не ниже порога, цена ниже EMA100 и EMA20 ниже EMA100.

Пороги взяты стандартные и НЕ подбирались под историю: определитель должен
описывать рынок, а не подгоняться под результат стратегий.

Используется лабораторией стратегий (python -m bot.backtest.lab): каждая сделка
помечается состоянием рынка на момент входа, и стратегия оценивается отдельно
по каждому состоянию. В живом боте пока только показывается (в сводке).
"""
from __future__ import annotations

import bisect
from typing import Dict, List, Optional

UP, DOWN, RANGE, PANIC = "UP", "DOWN", "RANGE", "PANIC"
STATES = (UP, RANGE, DOWN, PANIC)
LABELS = {UP: "рост", RANGE: "боковик", DOWN: "падение", PANIC: "паника"}
ICONS = {UP: "📈", RANGE: "↔️", DOWN: "📉", PANIC: "🌪"}

STATE_EMA_FAST = 20
STATE_EMA_SLOW = 100
STATE_ADX_PERIOD = 14
STATE_ADX_MIN = 18.0
STATE_PANIC_SHORT = 3
STATE_PANIC_LONG = 30
STATE_PANIC_RATIO = 1.8

D1 = 86_400_000


def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = (values[i] - e) * k + e
        out[i] = e
    return out


def true_ranges(candles: List[dict]) -> List[float]:
    out = [candles[0]['high'] - candles[0]['low']] if candles else []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]['close']
        out.append(max(c['high'] - c['low'], abs(c['high'] - p), abs(c['low'] - p)))
    return out


def adx_series(candles: List[dict], period: int = 14) -> List[Optional[float]]:
    """ADX Уайлдера: сила тренда независимо от направления (0–100)."""
    n = len(candles)
    out: List[Optional[float]] = [None] * n
    if n < 2 * period + 1:
        return out
    tr = true_ranges(candles)
    plus = [0.0] * n
    minus = [0.0] * n
    for i in range(1, n):
        up = candles[i]['high'] - candles[i - 1]['high']
        dn = candles[i - 1]['low'] - candles[i]['low']
        plus[i] = up if up > dn and up > 0 else 0.0
        minus[i] = dn if dn > up and dn > 0 else 0.0
    atr = sum(tr[1:period + 1])
    p = sum(plus[1:period + 1])
    m = sum(minus[1:period + 1])
    dx: List[float] = []
    adx = None
    for i in range(period, n):
        if i > period:
            atr = atr - atr / period + tr[i]
            p = p - p / period + plus[i]
            m = m - m / period + minus[i]
        pdi = 100 * p / atr if atr else 0.0
        mdi = 100 * m / atr if atr else 0.0
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0)
        if len(dx) == period:
            adx = sum(dx) / period
        elif len(dx) > period:
            adx = (adx * (period - 1) + dx[-1]) / period
        if adx is not None:
            out[i] = adx
    return out


def state_series(days: List[dict]) -> List[Optional[str]]:
    """Состояние рынка на закрытие каждого дня (None — пока не хватает истории)."""
    n = len(days)
    closes = [d['close'] for d in days]
    fast = ema_series(closes, STATE_EMA_FAST)
    slow = ema_series(closes, STATE_EMA_SLOW)
    adx = adx_series(days, STATE_ADX_PERIOD)
    tr = true_ranges(days)
    out: List[Optional[str]] = [None] * n
    for i in range(n):
        if slow[i] is None or fast[i] is None or adx[i] is None or i < STATE_PANIC_LONG:
            continue
        short = sum(tr[i - STATE_PANIC_SHORT + 1:i + 1]) / STATE_PANIC_SHORT
        long_ = sum(tr[i - STATE_PANIC_LONG + 1:i + 1]) / STATE_PANIC_LONG
        if long_ > 0 and short / long_ >= STATE_PANIC_RATIO:
            out[i] = PANIC
        elif adx[i] < STATE_ADX_MIN:
            out[i] = RANGE
        elif closes[i] > slow[i] and fast[i] > slow[i]:
            out[i] = UP
        elif closes[i] < slow[i] and fast[i] < slow[i]:
            out[i] = DOWN
        else:
            out[i] = RANGE
    return out


def current_state(days: List[dict]) -> Dict:
    """Состояние по последней закрытой дневной свече + числа для показа."""
    s = state_series(days)
    if not s or s[-1] is None:
        return {'state': None, 'reason': 'мало дневных свечей'}
    closes = [d['close'] for d in days]
    return {'state': s[-1], 'label': LABELS[s[-1]], 'icon': ICONS[s[-1]],
            'close': closes[-1], 'ema_slow': ema_series(closes, STATE_EMA_SLOW)[-1],
            'adx': adx_series(days, STATE_ADX_PERIOD)[-1]}


class StateLookup:
    """Состояние рынка на произвольный момент времени: по последнему ЗАКРЫТОМУ дню."""

    def __init__(self, days: List[dict]):
        self._close_times = [d['start'] + D1 for d in days]
        self._states = state_series(days)

    def at(self, ts: int) -> Optional[str]:
        i = bisect.bisect_right(self._close_times, ts) - 1
        return self._states[i] if i >= 0 else None
