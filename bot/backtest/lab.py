"""
Лаборатория стратегий: таблица «стратегия × состояние рынка».

Каждая стратегия проигрывается на истории по всем парам независимо (без общего
лимита позиций — оцениваем качество сигналов, а не портфель). Каждая сделка
помечается состоянием рынка на момент входа (bot/services/market_state.py), и
результат считается отдельно для роста, боковика, падения и паники.

Допуск стратегии в состоянии: не меньше MIN_TRADES сделок, средний результат > 0R
после комиссий, профит-фактор ≥ 1.2 и плюс в ОБЕИХ половинах периода (защита от
«повезло один раз»). Параметры стратегий стандартные и под историю не подбирались.

Запуск:
  python -m bot.backtest.lab                # 5 лет, 10 пар
  python -m bot.backtest.lab --days 1095 --offline

Допущения: вход по закрытию сигнальной свечи; стоп внутри свечи считается раньше
цели; комиссия 0.1% за сделку + проскальзывание 0.05% в обе стороны и для лонга,
и для шорта. Шорт считается по спотовым ценам (на бессрочном фьючерсе цена почти
та же); ставка финансирования не учтена — для шорта на падении она обычно в плюс.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from config import trading_config as cfg
from bot.services import market_state as MS
from bot.services import trend_strategy as T
from bot.services.dip_strategy import rsi_series

H4, D1 = 4 * 3_600_000, 86_400_000
MIN_TRADES = 20
DEFAULT_PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
                 "LINKUSDT", "AVAXUSDT", "LTCUSDT", "DOTUSDT"]
PAIR_SPREADS = [("ETHUSDT", "BTCUSDT"), ("SOLUSDT", "ETHUSDT"), ("SOLUSDT", "BTCUSDT"),
                ("LINKUSDT", "ETHUSDT"), ("AVAXUSDT", "SOLUSDT"), ("LTCUSDT", "BTCUSDT")]


@dataclass
class LabTrade:
    strategy: str
    pair: str
    side: int
    opened: int
    closed: int
    r: float
    exit: str
    state: Optional[str]
    entry: float = 0.0
    stop: float = 0.0            # действующий стоп на момент закрытия (с учётом трейлинга)
    exit_price: float = 0.0


# ----------------------------------------------------------------- индикаторы (ряды по индексу свечи)

def _atr(candles: List[dict], period: int = 14) -> List[Optional[float]]:
    tr = MS.true_ranges(candles)
    out: List[Optional[float]] = [None] * len(candles)
    s = 0.0
    for i, v in enumerate(tr):
        s += v
        if i >= period:
            s -= tr[i - period]
        if i >= period:
            out[i] = s / period
    return out


def _rolling(values: List[float], period: int, fn) -> List[Optional[float]]:
    """fn по окну из period ПРЕДЫДУЩИХ значений (текущая свеча не входит)."""
    return [fn(values[i - period:i]) if i >= period else None for i in range(len(values))]


def _mean_sd(values: List[float], period: int):
    mean: List[Optional[float]] = [None] * len(values)
    sd: List[Optional[float]] = [None] * len(values)
    s = s2 = 0.0
    for i, v in enumerate(values):
        s += v
        s2 += v * v
        if i >= period:
            s -= values[i - period]
            s2 -= values[i - period] ** 2
        if i >= period - 1:
            m = s / period
            mean[i], sd[i] = m, math.sqrt(max(0.0, s2 / period - m * m))
    return mean, sd


def _supertrend(candles: List[dict], period: int = 10, mult: float = 3.0):
    """Возвращает (линия, направление): +1 — цена над линией, −1 — под ней."""
    n = len(candles)
    atr = _atr(candles, period)
    line: List[Optional[float]] = [None] * n
    direction: List[int] = [0] * n
    up = dn = None
    for i in range(n):
        if atr[i] is None:
            continue
        c = candles[i]
        mid = (c['high'] + c['low']) / 2
        b_up, b_dn = mid + mult * atr[i], mid - mult * atr[i]
        prev_close = candles[i - 1]['close']
        up = b_up if up is None or b_up < up or prev_close > up else up
        dn = b_dn if dn is None or b_dn > dn or prev_close < dn else dn
        prev_dir = direction[i - 1] or 1
        d = prev_dir
        if prev_dir > 0 and c['close'] < dn:
            d, up = -1, b_up
        elif prev_dir < 0 and c['close'] > up:
            d, dn = 1, b_dn
        direction[i] = d
        line[i] = dn if d > 0 else up
    return line, direction


def indicators(candles: List[dict]) -> Dict[str, list]:
    closes = [c['close'] for c in candles]
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    mid, sd = _mean_sd(closes, 20)
    st_line, st_dir = _supertrend(candles)
    return {
        'close': closes, 'atr': _atr(candles, 14), 'rsi': rsi_series(closes, 14),
        'ema20': MS.ema_series(closes, 20), 'ema50': MS.ema_series(closes, 50),
        'ema100': MS.ema_series(closes, 100),
        'hi20': _rolling(highs, 20, max), 'lo20': _rolling(lows, 20, min),
        'mid': mid, 'upper': [None if m is None else m + 2 * s for m, s in zip(mid, sd)],
        'lower': [None if m is None else m - 2 * s for m, s in zip(mid, sd)],
        'st_line': st_line, 'st_dir': st_dir,
    }


# ----------------------------------------------------------------- симуляция одной стратегии на одной паре

@dataclass
class Spec:
    key: str
    title: str
    tf: str                      # 'D' или '240'
    side: int                    # +1 лонг, −1 шорт
    entry: Callable              # (I, candles, i) -> стоп или None
    exit: Callable               # (I, candles, i, pos) -> причина выхода или None (по закрытию свечи)
    trail: Optional[Callable] = None   # (I, candles, i, pos) -> новый стоп
    max_bars: Optional[int] = None
    family: str = ""


def _costs(entry: float) -> float:
    return entry * (2 * cfg.TRADING_FEE_RATE + cfg.SLIPPAGE_ESTIMATE)


def simulate(spec: Spec, pair: str, candles: List[dict], I: Dict[str, list], step: int,
             start_ts: int, end_ts: int, state_at: Callable[[int], Optional[str]]) -> List[LabTrade]:
    trades: List[LabTrade] = []
    pos = None
    slip = cfg.SLIPPAGE_ESTIMATE
    side = spec.side

    def close(price: float, t: int, kind: str):
        nonlocal pos
        gross = side * (price - pos['entry'])
        risk = abs(pos['entry'] - pos['stop0']) + _costs(pos['entry'])
        pnl = gross - pos['entry'] * 2 * cfg.TRADING_FEE_RATE
        trades.append(LabTrade(spec.key, pair, side, pos['opened'], t, pnl / risk if risk > 0 else 0.0,
                               kind, pos['state'], entry=pos['entry'], stop=pos['stop'], exit_price=price))
        pos = None

    for i, bar in enumerate(candles):
        t = bar['start'] + step
        if pos:
            hit = bar['low'] <= pos['stop'] if side > 0 else bar['high'] >= pos['stop']
            if hit:
                px = min(pos['stop'], bar['open']) if side > 0 else max(pos['stop'], bar['open'])
                close(px * (1 - slip * side), t, 'stop' if pos['stop'] == pos['stop0'] else 'trail')
                continue
            pos['bars'] += 1
            pos['best'] = max(pos['best'], bar['close']) if side > 0 else min(pos['best'], bar['close'])
            why = spec.exit(I, candles, i, pos)
            if not why and spec.max_bars and pos['bars'] >= spec.max_bars:
                why = 'time'
            if why:
                close(bar['close'] * (1 - slip * side), t, why)
                continue
            if spec.trail:
                new = spec.trail(I, candles, i, pos)
                if new is not None:
                    pos['stop'] = max(pos['stop'], new) if side > 0 else min(pos['stop'], new)
            continue
        if not (start_ts <= t < end_ts):
            continue
        stop = spec.entry(I, candles, i)
        if stop is None:
            continue
        entry = bar['close'] * (1 + slip * side)
        if (side > 0 and stop >= entry) or (side < 0 and stop <= entry):
            continue
        pos = {'entry': entry, 'stop': stop, 'stop0': stop, 'opened': t, 'bars': 0,
               'best': bar['close'], 'state': state_at(t), 'atr': I['atr'][i]}
    if pos:
        close(candles[-1]['close'], candles[-1]['start'] + step, 'end')
    return trades


# ----------------------------------------------------------------- стратегии

def _ok(*vals) -> bool:
    return all(v is not None for v in vals)


def _chandelier(mult: float, side: int):
    def trail(I, c, i, pos):
        a = I['atr'][i] or pos['atr']
        return pos['best'] - mult * a if side > 0 else pos['best'] + mult * a
    return trail


def _no_exit(I, c, i, pos):
    return None


def trend_long_entry(I, c, i):
    """Текущая стратегия бота: пробой 20-дн. максимума выше EMA100."""
    if not _ok(I['hi20'][i], I['ema100'][i], I['atr'][i]):
        return None
    close = c[i]['close']
    return close - 2 * I['atr'][i] if close > I['hi20'][i] and close > I['ema100'][i] else None


def trend_short_entry(I, c, i):
    """Зеркало: пробой 20-дн. минимума ниже EMA100."""
    if not _ok(I['lo20'][i], I['ema100'][i], I['atr'][i]):
        return None
    close = c[i]['close']
    return close + 2 * I['atr'][i] if close < I['lo20'][i] and close < I['ema100'][i] else None


def retest_long_entry(I, c, i):
    """Пробой с ретестом: за последние 10 дней был пробой уровня, сегодня цена
    вернулась к уровню (минимум не выше уровень + 0.25 ATR) и закрылась над ним."""
    if not _ok(I['ema100'][i], I['atr'][i]) or i < 12:
        return None
    close, atr = c[i]['close'], I['atr'][i]
    if close <= I['ema100'][i]:
        return None
    for b in range(i - 1, i - 11, -1):
        level = I['hi20'][b]
        if level is None or c[b]['close'] <= level:
            continue
        if b >= 1 and I['hi20'][b - 1] is not None and c[b - 1]['close'] > I['hi20'][b - 1]:
            continue                       # берём первый день пробоя, а не продолжение
        if c[i]['low'] <= level + 0.25 * atr and close > level and close > c[i]['open']:
            return level - 1.5 * atr
        return None
    return None


def supertrend_long_entry(I, c, i):
    if i < 1 or not _ok(I['st_line'][i], I['ema100'][i]):
        return None
    flipped = I['st_dir'][i] > 0 and I['st_dir'][i - 1] < 0
    return I['st_line'][i] if flipped and c[i]['close'] > I['ema100'][i] else None


def supertrend_exit(I, c, i, pos):
    return 'flip' if I['st_dir'][i] < 0 else None


def supertrend_trail(I, c, i, pos):
    return I['st_line'][i] if I['st_dir'][i] > 0 else None


def ema_cross_long_entry(I, c, i):
    if i < 1 or not _ok(I['ema20'][i - 1], I['ema50'][i - 1], I['atr'][i]):
        return None
    crossed = I['ema20'][i] > I['ema50'][i] and I['ema20'][i - 1] <= I['ema50'][i - 1]
    return c[i]['close'] - 2 * I['atr'][i] if crossed else None


def ema_cross_exit(I, c, i, pos):
    return 'cross' if I['ema20'][i] < I['ema50'][i] else None


def meanrev_long_entry(I, c, i):
    """Покупка на просадке: RSI ≤ 30 и закрытие под нижней полосой Боллинджера (4h)."""
    if not _ok(I['rsi'][i], I['lower'][i], I['atr'][i]):
        return None
    close = c[i]['close']
    return close - 2 * I['atr'][i] if I['rsi'][i] <= 30 and close < I['lower'][i] else None


def meanrev_short_entry(I, c, i):
    """Продажа на всплеске: RSI ≥ 70 и закрытие над верхней полосой (4h)."""
    if not _ok(I['rsi'][i], I['upper'][i], I['atr'][i]):
        return None
    close = c[i]['close']
    return close + 2 * I['atr'][i] if I['rsi'][i] >= 70 and close > I['upper'][i] else None


def meanrev_long_exit(I, c, i, pos):
    return 'target' if I['mid'][i] is not None and c[i]['close'] >= I['mid'][i] else None


def meanrev_short_exit(I, c, i, pos):
    return 'target' if I['mid'][i] is not None and c[i]['close'] <= I['mid'][i] else None


SPECS: List[Spec] = [
    Spec('trend_long', 'Тренд: пробой вверх (текущая)', 'D', +1, trend_long_entry, _no_exit,
         _chandelier(3.0, +1), family='тренд'),
    Spec('trend_short', 'Тренд: пробой вниз (шорт)', 'D', -1, trend_short_entry, _no_exit,
         _chandelier(3.0, -1), family='тренд'),
    Spec('retest_long', 'Пробой с ретестом', 'D', +1, retest_long_entry, _no_exit,
         _chandelier(3.0, +1), family='тренд'),
    Spec('supertrend_long', 'Supertrend (10, 3)', 'D', +1, supertrend_long_entry, supertrend_exit,
         supertrend_trail, family='тренд'),
    Spec('ema_cross_long', 'Пересечение EMA20/EMA50', 'D', +1, ema_cross_long_entry, ema_cross_exit,
         family='тренд'),
    Spec('meanrev_long', 'Возврат к средней: покупка просадки (4h)', '240', +1, meanrev_long_entry,
         meanrev_long_exit, max_bars=30, family='возврат к средней'),
    Spec('meanrev_short', 'Возврат к средней: продажа всплеска (4h)', '240', -1, meanrev_short_entry,
         meanrev_short_exit, max_bars=30, family='возврат к средней'),
]


# ----------------------------------------------------------------- парная торговля

def simulate_pairs(a: str, b: str, da: List[dict], db: List[dict], start_ts: int, end_ts: int,
                   state_at, window: int = 30, z_in: float = 2.0, z_stop: float = 3.5,
                   max_days: int = 20) -> List[LabTrade]:
    """
    Рыночно-нейтральная пара на дневных свечах: спред = ln(A/B). Когда спред ушёл
    на z_in сигм от средней за window дней — ставим на возврат (лонг отставшей,
    шорт убежавшей, равные суммы). Выход — возврат к средней, стоп — z_stop сигм,
    либо max_days. Комиссии — за четыре сделки.
    """
    by_b = {d['start']: d for d in db}
    rows = [(d['start'], d['close'], by_b[d['start']]['close']) for d in da if d['start'] in by_b]
    spread = [math.log(x / y) for _, x, y in rows]
    mean, sd = _mean_sd(spread, window)
    fee = 2 * (2 * cfg.TRADING_FEE_RATE + cfg.SLIPPAGE_ESTIMATE)      # две ноги, полный круг
    trades: List[LabTrade] = []
    pos = None
    for i in range(1, len(rows)):
        t = rows[i][0] + D1
        m, s = mean[i - 1], sd[i - 1]                 # статистика без сегодняшнего дня
        if m is None or not s:
            continue
        z = (spread[i] - m) / s
        if pos:
            pos['days'] += 1
            move = pos['dir'] * (spread[i] - pos['spread'])       # > 0 — в нашу сторону
            why = None
            if pos['dir'] * z >= 0:
                why = 'target'
            elif -move >= pos['risk']:
                why = 'stop'
            elif pos['days'] >= max_days:
                why = 'time'
            if why:
                trades.append(LabTrade('pairs', f"{a[:-4]}/{b[:-4]}", pos['dir'], pos['opened'], t,
                                       (move - fee) / (pos['risk'] + fee), why, pos['state']))
                pos = None
            continue
        if not (start_ts <= t < end_ts) or abs(z) < z_in or abs(z) >= z_stop:
            continue
        pos = {'dir': -1 if z > 0 else 1, 'spread': spread[i], 'risk': (z_stop - abs(z)) * s,
               'opened': t, 'days': 0, 'state': state_at(t)}
    return trades


# ----------------------------------------------------------------- сводка

def summarize(trades: List[LabTrade]) -> Dict:
    n = len(trades)
    if not n:
        return {'n': 0, 'avg_r': 0.0, 'pf': 0.0, 'win': 0.0, 'sum_r': 0.0}
    win = sum(t.r for t in trades if t.r > 0)
    loss = -sum(t.r for t in trades if t.r <= 0)
    return {'n': n, 'avg_r': sum(t.r for t in trades) / n,
            'pf': win / loss if loss > 0 else float('inf'),
            'win': sum(1 for t in trades if t.r > 0) / n, 'sum_r': sum(t.r for t in trades)}


def admitted(trades: List[LabTrade], mid_ts: int) -> Tuple[bool, str]:
    s = summarize(trades)
    if s['n'] < MIN_TRADES:
        return False, f"мало сделок ({s['n']})"
    first = summarize([t for t in trades if t.opened < mid_ts])
    second = summarize([t for t in trades if t.opened >= mid_ts])
    if s['avg_r'] <= 0:
        return False, "в минусе"
    if s['pf'] < 1.2:
        return False, f"профит-фактор {s['pf']:.2f}"
    if first['n'] < 5 or second['n'] < 5:
        return False, "сделки только в одной половине периода"
    if first['avg_r'] <= 0 or second['avg_r'] <= 0:
        return False, f"нестабильно: {first['avg_r']:+.2f}R → {second['avg_r']:+.2f}R"
    return True, f"{first['avg_r']:+.2f}R → {second['avg_r']:+.2f}R"


def run_lab(data: Dict[str, Dict[str, List[dict]]], pairs: List[str], start_ts: int, end_ts: int) -> Dict:
    days = {p: T.daily_from_4h(data[p]['240']) for p in pairs}
    lookup = MS.StateLookup(days['BTCUSDT'])
    out: Dict[str, List[LabTrade]] = {}
    ind = {p: {'D': indicators(days[p]), '240': indicators(data[p]['240'])} for p in pairs}
    for spec in SPECS:
        trades: List[LabTrade] = []
        for p in pairs:
            candles = days[p] if spec.tf == 'D' else data[p]['240']
            trades += simulate(spec, p, candles, ind[p][spec.tf], D1 if spec.tf == 'D' else H4,
                               start_ts, end_ts, lookup.at)
        out[spec.key] = trades
    pt: List[LabTrade] = []
    for a, b in PAIR_SPREADS:
        if a in days and b in days:
            pt += simulate_pairs(a, b, days[a], days[b], start_ts, end_ts, lookup.at)
    out['pairs'] = pt
    states = [s for s, d in zip(MS.state_series(days['BTCUSDT']), days['BTCUSDT'])
              if s and start_ts <= d['start'] + D1 < end_ts]
    return {'trades': out, 'state_days': {s: states.count(s) for s in MS.STATES}, 'total_days': len(states),
            'now': MS.current_state(days['BTCUSDT'])}


def report(res: Dict, pairs: List[str], start_ts: int, end_ts: int) -> List[str]:
    fmt = lambda ts: datetime.fromtimestamp(ts / 1000, timezone.utc).strftime('%Y-%m-%d')
    mid_ts = (start_ts + end_ts) // 2
    titles = {s.key: s.title for s in SPECS}
    titles['pairs'] = 'Парная торговля (спред двух монет)'
    total = res['total_days'] or 1
    lines = [
        "# Лаборатория стратегий: стратегия × состояние рынка", "",
        f"Период: {fmt(start_ts)} — {fmt(end_ts)}. Пары: {', '.join(p[:-4] for p in pairs)}.",
        f"Комиссия {cfg.TRADING_FEE_RATE * 100:.2f}% + проскальзывание {cfg.SLIPPAGE_ESTIMATE * 100:.2f}% на сделку. "
        "Результат — в R (1R = риск сделки до стопа, с комиссиями).", "",
        "## Сколько времени рынок был в каждом состоянии", "",
        "| Состояние | Дней | Доля |", "|---|---|---|",
    ]
    for s in MS.STATES:
        lines.append(f"| {MS.ICONS[s]} {MS.LABELS[s]} | {res['state_days'][s]} | {res['state_days'][s] / total * 100:.0f}% |")
    now = res['now']
    if now.get('state'):
        lines += ["", f"Сейчас: **{now['icon']} {now['label']}** (BTC {now['close']:,.0f}, "
                      f"EMA100 {now['ema_slow']:,.0f}, ADX {now['adx']:.0f}).", ""]
    lines += ["## Таблица", "",
              f"В ячейке: сделок / средний результат / профит-фактор. ✅ — допуск: от {MIN_TRADES} сделок, "
              "плюс после комиссий, профит-фактор от 1.2, плюс в обеих половинах периода.", "",
              "| Стратегия | Всего | " + " | ".join(f"{MS.ICONS[s]} {MS.LABELS[s]}" for s in MS.STATES) + " |",
              "|---|---|" + "---|" * len(MS.STATES)]
    admitted_rows = []
    for key, trades in res['trades'].items():
        a = summarize(trades)
        cells = []
        for s in MS.STATES:
            sub = [t for t in trades if t.state == s]
            m = summarize(sub)
            ok, why = admitted(sub, mid_ts)
            if not m['n']:
                cells.append("—")
                continue
            pf = "∞" if m['pf'] == float('inf') else f"{m['pf']:.2f}"
            cells.append(f"{'✅ ' if ok else ''}{m['n']} / {m['avg_r']:+.2f}R / {pf}")
            if ok:
                admitted_rows.append((titles[key], s, m, why))
        pf = "∞" if a['pf'] == float('inf') else f"{a['pf']:.2f}"
        lines.append(f"| {titles[key]} | {a['n']} / {a['avg_r']:+.2f}R / {pf} | " + " | ".join(cells) + " |")
    from bot.services import admission as A
    lines += ["", "## Строгая проверка стратегии целиком", "",
              f"Допуск к торговле: от {getattr(cfg, 'ADMISSION_MIN_TRADES', 100)} сделок, плюс после комиссий, "
              f"вероятность случайного плюса ниже {getattr(cfg, 'ADMISSION_MAX_P', 0.05) * 100:.0f}% "
              "(перемешивание сделок), плюс в обеих половинах периода. Вероятности занижены: монеты ходят "
              "вместе, независимых сделок меньше, чем в таблице.", "",
              "| Стратегия | Сделок | Средний R | Вероятность случайного плюса | Половины периода | Вывод |",
              "|---|---|---|---|---|---|"]
    for key, trades in res['trades'].items():
        ev = A.evaluate([(t.opened, t.r) for t in trades], mid_ts)
        verdict = {A.LIVE: "✅ допуск", A.PAPER: "📝 на бумагу", A.OFF: "⛔ запрет"}[ev['verdict']]
        lines.append(f"| {titles[key]} | {ev['n']} | {ev['avg_r']:+.2f} | {ev['p'] * 100:.1f}% | "
                     f"{ev['h1']:+.2f}R → {ev['h2']:+.2f}R | {verdict}: {ev['why']} |")
    lines += ["", "## Ячейки таблицы, прошедшие мягкий допуск", "",
              "Справочно: разбивка по состояниям дробит сделки на мелкие группы, часть ячеек проходит случайно.", ""]
    if admitted_rows:
        lines += ["| Стратегия | Состояние | Сделок | Средний R | Прибыльных | Сумма R | Первая → вторая половина |",
                  "|---|---|---|---|---|---|---|"]
        for title, s, m, why in sorted(admitted_rows, key=lambda x: -x[2]['sum_r']):
            lines.append(f"| {title} | {MS.LABELS[s]} | {m['n']} | {m['avg_r']:+.2f} | {m['win'] * 100:.0f}% | "
                         f"{m['sum_r']:+.1f} | {why} |")
    else:
        lines.append("Ни одна ячейка не прошла.")
    lines += ["", "## Почему остальные не прошли", ""]
    for key, trades in res['trades'].items():
        parts = []
        for s in MS.STATES:
            sub = [t for t in trades if t.state == s]
            ok, why = admitted(sub, mid_ts)
            if not ok and sub:
                parts.append(f"{MS.LABELS[s]} — {why}")
        if parts:
            lines.append(f"- **{titles[key]}**: " + "; ".join(parts))
    lines.append("")
    return lines


def main(argv=None) -> int:
    from .data import fetch_klines
    ap = argparse.ArgumentParser(prog="python -m bot.backtest.lab")
    ap.add_argument("--days", type=int, default=1825)
    ap.add_argument("--pairs", default=",".join(DEFAULT_PAIRS))
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    if 'BTCUSDT' not in pairs:
        pairs.insert(0, 'BTCUSDT')
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (args.days + 260) * D1
    data = {}
    for p in pairs:
        print(f"📥 {p}...", flush=True)
        try:
            rows = fetch_klines(p, "240", start_ms, end_ms, offline=args.offline)
        except Exception as e:
            print(f"  ⚠️ {p} пропущена: {str(e)[:100]}")
            continue
        # Монета берётся с того дня, с которого у Bybit есть её история (не меньше 3 лет):
        # у многих пар спот-история короче 5 лет, а выбрасывать их — терять статистику.
        if p == 'BTCUSDT' or len(rows) >= (min(args.days, 1095) + 150) * 6 * 0.97:
            data[p] = {"240": rows}
            first = datetime.fromtimestamp(rows[0]['start'] / 1000, timezone.utc).strftime('%Y-%m-%d')
            print(f"  история с {first}")
        else:
            print(f"  ⚠️ {p}: история короче 3 лет ({len(rows)} свечей) — пропущена")
        if not args.offline:
            time.sleep(1.0)
    pairs = [p for p in pairs if p in data]
    if 'BTCUSDT' not in data:
        print("❌ Нет данных BTCUSDT")
        return 1
    end_ts = data['BTCUSDT']['240'][-1]['start'] + H4
    start_ts = end_ts - args.days * D1
    res = run_lab(data, pairs, start_ts, end_ts)
    lines = report(res, pairs, start_ts, end_ts)
    out = Path(args.out) if args.out else Path(__file__).resolve().parents[2] / "data" / "backtest" / \
        (datetime.now().strftime("%Y-%m-%d_%H%M") + "_lab")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    import csv
    with (out / "trades.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "pair", "side", "opened_utc", "closed_utc", "r", "exit", "state"])
        fmt = lambda ts: datetime.fromtimestamp(ts / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M')
        for key, trades in res['trades'].items():
            for t in trades:
                w.writerow([t.strategy, t.pair, t.side, fmt(t.opened), fmt(t.closed), f"{t.r:.3f}", t.exit, t.state])
    print("\n".join(lines))
    print(f"\n📄 Отчёт: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
