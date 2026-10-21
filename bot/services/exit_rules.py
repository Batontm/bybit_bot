"""
Правила входа/выхода в единицах R — общие для живого бота и бэктестера.

R — риск на единицу монеты: вход − начальный стоп.
Издержки полного круга (комиссия входа + выхода + проскальзывание) считаются
отдельно, чтобы «безубыток» и фильтр сделок учитывали реальные деньги.

Живой бот:   TP1 (часть позиции на +EXIT_PARTIAL_R) и TP2 (остаток на +FUNNEL_TP_R)
             стоят лимитными ордерами на бирже; безубыток/трейлинг/время — задачи бота.
Бэктестер:   simulate_bar() проигрывает те же правила на 15-минутных свечах.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from config import trading_config as cfg


# ----------------------------------------------------------------- издержки

def cost_per_unit(entry: float) -> float:
    """Комиссии полного круга + проскальзывание на 1 монету."""
    return entry * (2 * cfg.TRADING_FEE_RATE + cfg.SLIPPAGE_ESTIMATE)


def breakeven_stop(entry: float) -> float:
    """Стоп «безубытка» с запасом на комиссии."""
    return entry * (1 + cfg.EXIT_BREAKEVEN_FEE_BUFFER)


def net_rr(entry: float, stop: float, target: float) -> float:
    """Чистое отношение прибыль/риск после издержек."""
    c = cost_per_unit(entry)
    risk = entry - stop + c
    return (target - entry - c) / risk if risk > 0 else 0.0


def fee_filter(entry: float, stop: float, target: float) -> Tuple[bool, str]:
    """Пропускать сделки, где комиссии съедают слишком большую часть риска/прибыли."""
    r = entry - stop
    if r <= 0:
        return False, "стоп выше входа"
    c = cost_per_unit(entry)
    share = c / r
    if share > cfg.FUNNEL_MAX_FEE_SHARE:
        return False, f"комиссии {share * 100:.0f}% риска > {cfg.FUNNEL_MAX_FEE_SHARE * 100:.0f}% (стоп слишком близко)"
    rr = net_rr(entry, stop, target)
    if rr < cfg.FUNNEL_MIN_NET_RR:
        return False, f"чистый R/R {rr:.2f} < {cfg.FUNNEL_MIN_NET_RR}"
    return True, f"чистый R/R {rr:.2f}, комиссии {share * 100:.0f}% риска"


def position_size(equity: float, risk_fraction: float, entry: float, stop: float) -> float:
    """Количество монет: при срабатывании стопа теряем risk_fraction капитала С УЧЁТОМ издержек."""
    per_unit = (entry - stop) + cost_per_unit(entry)
    if per_unit <= 0 or equity <= 0:
        return 0.0
    return equity * risk_fraction / per_unit


# ----------------------------------------------------------------- уровни

@dataclass
class Levels:
    entry: float
    stop: float              # начальный стоп
    r: float
    partial: Optional[float]  # цена TP1 (None — частичная фиксация выключена)
    target: float            # TP2 / полный тейк
    breakeven_trigger: float


def levels(entry: float, stop: float, tp_r: Optional[float] = None) -> Levels:
    r = entry - stop
    tp_r = cfg.FUNNEL_TP_R if tp_r is None else tp_r
    partial = entry + cfg.EXIT_PARTIAL_R * r if cfg.EXIT_PARTIAL_ENABLED else None
    return Levels(entry=entry, stop=stop, r=r, partial=partial, target=entry + tp_r * r,
                  breakeven_trigger=entry + cfg.EXIT_BREAKEVEN_R * r)


def trail_stop(current_stop: float, reference_price: float, atr: float) -> float:
    """Трейлинг: стоп = цена − k×ATR, только вверх."""
    if not atr or atr <= 0:
        return current_stop
    return max(current_stop, reference_price - cfg.EXIT_TRAIL_ATR_MULT * atr)


def time_exit_due(age_hours: float, price: float, entry: float, r: float) -> bool:
    """Позиция «застряла»: прошло EXIT_TIME_HOURS, а цена не дошла до +EXIT_TIME_MIN_R."""
    if not cfg.TIME_EXIT_ENABLED or r <= 0:
        return False
    return age_hours >= cfg.EXIT_TIME_HOURS and price < entry + cfg.EXIT_TIME_MIN_R * r


# ----------------------------------------------------------------- симуляция (бэктест)

@dataclass
class SimPosition:
    pair: str
    entry: float
    qty: float
    lv: Levels
    atr: float
    opened_ts: int                      # мс
    stop: float = 0.0
    remaining: float = 0.0
    partial_done: bool = False
    highest: float = 0.0
    fills: List[Tuple[str, float, float]] = field(default_factory=list)  # (тип, цена, кол-во)

    def __post_init__(self):
        self.stop = self.stop or self.lv.stop
        self.remaining = self.remaining or self.qty
        self.highest = self.highest or self.entry


def simulate_bar(pos: SimPosition, bar: dict) -> bool:
    """
    Проиграть одну закрытую свечу (open/high/low/close/start). Возвращает True, если
    позиция полностью закрыта. Консервативно: если в одной свече достижимы и стоп,
    и цель — считаем, что первым сработал стоп.
    """
    slip = cfg.SLIPPAGE_ESTIMATE

    def sell(kind: str, price: float, qty: float):
        pos.fills.append((kind, price, qty))
        pos.remaining -= qty

    # 1. Стоп (рыночный по триггеру; гэп ниже стопа — исполнение по open)
    if bar['low'] <= pos.stop:
        px = min(pos.stop, bar['open']) * (1 - slip)
        sell('stop' if not pos.partial_done and pos.stop < pos.entry else 'stop_be_or_trail', px, pos.remaining)
        return True

    # 2. TP1 — частичная фиксация (лимит)
    if pos.lv.partial is not None and not pos.partial_done and bar['high'] >= pos.lv.partial:
        part = pos.qty * cfg.EXIT_PARTIAL_FRACTION
        sell('tp1', pos.lv.partial, part)
        pos.partial_done = True
        pos.stop = max(pos.stop, breakeven_stop(pos.entry))

    # 3. TP2 / тейк (лимит)
    if bar['high'] >= pos.lv.target:
        sell('tp', pos.lv.target, pos.remaining)
        return True

    # 4. Безубыток
    if cfg.BREAKEVEN_ENABLED and bar['high'] >= pos.lv.breakeven_trigger:
        pos.stop = max(pos.stop, breakeven_stop(pos.entry))

    # 5. Трейлинг остатка после TP1 (по закрытию свечи, как в живом боте)
    pos.highest = max(pos.highest, bar['high'])
    if cfg.TRAILING_ENABLED and pos.partial_done:
        pos.stop = trail_stop(pos.stop, bar['close'], pos.atr)

    # 6. Выход по времени — по закрытию свечи
    age_h = (bar['start'] + 15 * 60_000 - pos.opened_ts) / 3_600_000
    if time_exit_due(age_h, bar['close'], pos.entry, pos.lv.r):
        sell('time', bar['close'] * (1 - slip), pos.remaining)
        return True
    return False


def realized_pnl(pos: SimPosition) -> float:
    """PnL закрытой симулированной позиции с комиссиями входа и выходов."""
    fee = cfg.TRADING_FEE_RATE
    buy_cost = pos.entry * pos.qty * (1 + fee)
    proceeds = sum(px * q * (1 - fee) for _, px, q in pos.fills)
    return proceeds - buy_cost
