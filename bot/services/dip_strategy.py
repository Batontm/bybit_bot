"""
Стратегия «покупка на просадке» (возврат к средней) на 4-часовых свечах.

Идея — противоположная трендовой: покупаем не пробой вверх, а сильное падение,
и продаём, когда цена возвращается к своей средней.

Правила:
  вход   — закрытая свеча 4h: RSI(14) ≤ DIP_RSI_MAX и close ниже нижней полосы
           Боллинджера (DIP_BB_PERIOD, DIP_BB_STD); при DIP_TREND_EMA > 0 —
           только если цена выше длинной EMA (просадка внутри растущего рынка);
  стоп   — вход − DIP_STOP_ATR × ATR(14) 4h;
  выход  — закрытие свечи на/выше средней полосы (SMA20) либо через
           DIP_MAX_BARS свечей, если возврата не случилось.
Размер позиции — от стопа с учётом комиссий (exit_rules.position_size).

ВНИМАНИЕ: в живом боте стратегия НЕ подключена. Здесь только чистые функции
для бэктестера (python -m bot.backtest --strategy dip). Включать в торговлю
можно только если walk-forward покажет плюс после комиссий.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from config import trading_config as cfg

H4 = 4 * 3_600_000


def rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """RSI Уайлдера для каждой свечи (None, пока не хватает данных)."""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain /= period
    loss /= period
    out[period] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = (gain * (period - 1) + max(d, 0.0)) / period
        loss = (loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def _rolling_mean_sd(values: List[float], period: int) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    n = len(values)
    mean: List[Optional[float]] = [None] * n
    sd: List[Optional[float]] = [None] * n
    s = s2 = 0.0
    for i, v in enumerate(values):
        s += v
        s2 += v * v
        if i >= period:
            old = values[i - period]
            s -= old
            s2 -= old * old
        if i >= period - 1:
            m = s / period
            mean[i] = m
            sd[i] = math.sqrt(max(0.0, s2 / period - m * m))
    return mean, sd


def _atr_series(candles: List[dict], period: int = 14) -> List[Optional[float]]:
    """ATR как среднее True Range за period свечей (как TechnicalIndicators.atr_series)."""
    n = len(candles)
    out: List[Optional[float]] = [None] * n
    trs: List[float] = []
    s = 0.0
    for i in range(1, n):
        c, p = candles[i], candles[i - 1]['close']
        tr = max(c['high'] - c['low'], abs(c['high'] - p), abs(c['low'] - p))
        trs.append(tr)
        s += tr
        if len(trs) > period:
            s -= trs[-period - 1]
        if len(trs) >= period:
            out[i] = s / period
    return out


def _ema_series(values: List[float], period: int) -> List[Optional[float]]:
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, n):
        e = (values[i] - e) * k + e
        out[i] = e
    return out


def indicators(candles: List[dict]) -> Dict[str, list]:
    """Все ряды, нужные стратегии, выровненные по индексам свечей."""
    closes = [c['close'] for c in candles]
    mid, sd = _rolling_mean_sd(closes, cfg.DIP_BB_PERIOD)
    lower = [None if m is None else m - cfg.DIP_BB_STD * s for m, s in zip(mid, sd)]
    return {
        'rsi': rsi_series(closes, 14),
        'mid': mid,
        'lower': lower,
        'atr': _atr_series(candles, 14),
        'trend': _ema_series(closes, cfg.DIP_TREND_EMA) if cfg.DIP_TREND_EMA > 0 else [None] * len(closes),
    }


def entry_at(candles: List[dict], ind: Dict[str, list], i: int) -> Tuple[bool, Dict]:
    """Сигнал на закрытой свече с индексом i."""
    info: Dict = {}
    rsi, mid, lower, atr = ind['rsi'][i], ind['mid'][i], ind['lower'][i], ind['atr'][i]
    if rsi is None or mid is None or lower is None or not atr:
        info['reason'] = 'мало свечей'
        return False, info
    c = candles[i]
    close = c['close']
    info.update({'close': close, 'rsi': rsi, 'mid': mid, 'lower': lower, 'atr': atr})
    if rsi > cfg.DIP_RSI_MAX:
        info['reason'] = f'RSI {rsi:.0f} > {cfg.DIP_RSI_MAX}'
        return False, info
    if close >= lower:
        info['reason'] = 'цена не ниже нижней полосы'
        return False, info
    if cfg.DIP_TREND_EMA > 0:
        trend = ind['trend'][i]
        if trend is None or close <= trend:
            info['reason'] = f'цена ниже EMA{cfg.DIP_TREND_EMA} — падающий рынок'
            return False, info
    if cfg.DIP_CONFIRM_GREEN and close <= c['open']:
        info['reason'] = 'нет разворотной (зелёной) свечи'
        return False, info
    info['stop'] = close - cfg.DIP_STOP_ATR * atr
    return True, info


def entry_signal(candles: List[dict]) -> Tuple[bool, Dict]:
    """Сигнал на последней ЗАКРЫТОЙ свече (для живого бота / ручной проверки)."""
    if len(candles) < max(cfg.DIP_BB_PERIOD, 15, cfg.DIP_TREND_EMA) + 2:
        return False, {'reason': 'мало свечей'}
    return entry_at(candles, indicators(candles), len(candles) - 1)
