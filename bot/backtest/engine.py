"""
Симуляция воронки входа и правил выхода на исторических свечах.

Что проверяется (те же функции, что у живого бота):
  светофор BTC 4H → тренд 4h → [откат 1h, сила к BTC] → триггер 15m →
  план сделки (стоп за откатом, фильтр комиссий) → выходы по exit_rules.
Что НЕ проверяется: стакан, поток сделок, OI, funding, отбор по спреду/обороту —
исторических данных по ним у Bybit нет. Эти компоненты оцениваются на демо по
журналу entry_signals.

Допущения (консервативные):
  • вход по закрытию 15m свечи-триггера (живой бот ставит лимит по биду);
  • если в одной свече достижимы и стоп, и цель — первым считается стоп;
  • комиссия 0.1% на каждую сделку + проскальзывание на рыночных выходах;
  • список пар фиксирован (нет «выживших» монет — но и нет делистингов).
"""
from __future__ import annotations

import bisect
import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from config import trading_config as cfg
from bot.services import entry_funnel as F
from bot.services import exit_rules as X
from bot.services.market_regime_service import regime_metrics, regime_allowed

H4, H1, M15 = 4 * 3_600_000, 3_600_000, 15 * 60_000


@contextmanager
def override(params: Dict):
    """Временно подменить параметры config.trading_config (для перебора в walk-forward)."""
    old = {k: getattr(cfg, k) for k in params}
    try:
        for k, v in params.items():
            setattr(cfg, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(cfg, k, v)


@dataclass
class Trade:
    pair: str
    opened_ts: int
    closed_ts: int
    entry: float
    stop: float
    qty: float
    risk_usd: float
    pnl: float
    r_multiple: float
    exit_kind: str
    trigger: str
    pullback_ok: bool
    rs_ok: bool
    score: int


@dataclass
class Result:
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Tuple[int, float]] = field(default_factory=list)
    start_equity: float = 0.0
    halted_at: Optional[int] = None
    signals_seen: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)


class _Series:
    """Свечи одного таймфрейма с быстрым поиском «закрытых к моменту T»."""

    def __init__(self, candles: List[dict], step: int):
        self.c = candles
        self.step = step
        self.close_times = [x['start'] + step for x in candles]

    def upto(self, t: int, n: int) -> List[dict]:
        i = bisect.bisect_right(self.close_times, t)
        return self.c[max(0, i - n):i]

    def index(self, t: int) -> int:
        return bisect.bisect_right(self.close_times, t)


def run(data: Dict[str, Dict[str, List[dict]]], pairs: Iterable[str], *,
        start_ts: int, end_ts: int, equity: float = 10_000.0,
        require_pullback: bool = False, require_rs: bool = False,
        use_guard: bool = True, use_regime: bool = True) -> Result:
    """Прогон на интервале [start_ts, end_ts). Свечи до start_ts — прогрев индикаторов."""
    pairs = [p for p in pairs if p in data]
    series = {p: {tf: _Series(data[p][tf], st) for tf, st in (("240", H4), ("60", H1), ("15", M15))}
              for p in set(pairs) | {'BTCUSDT'} if p in data}
    btc = series['BTCUSDT']

    res = Result(start_equity=equity)
    open_pos: Dict[str, Tuple[X.SimPosition, dict]] = {}
    cache: Dict[Tuple[str, str, int], tuple] = {}
    loss_streak, pause_until, peak = 0, 0, equity
    day_key, day_pnl = None, 0.0

    def skip(reason: str):
        res.skipped[reason] = res.skipped.get(reason, 0) + 1

    # общая шкала времени — закрытия 15m свечей BTC (самый длинный ряд)
    timeline = [t for t in btc["15"].close_times if start_ts <= t < end_ts]

    for t in timeline:
        # ---------------- сопровождение позиций свечой, закрывшейся в t
        for pair in list(open_pos):
            pos, meta = open_pos[pair]
            bar_list = series[pair]["15"].upto(t, 1)
            if not bar_list or bar_list[-1]['start'] + M15 != t:
                continue
            if X.simulate_bar(pos, bar_list[-1]):
                pnl = X.realized_pnl(pos)
                equity += pnl
                kind = pos.fills[-1][0] if pos.fills else 'stop'
                if any(f[0] == 'tp1' for f in pos.fills) and kind != 'tp':
                    kind = 'tp1+' + kind
                res.trades.append(Trade(
                    pair=pair, opened_ts=pos.opened_ts, closed_ts=t, entry=pos.entry, stop=pos.lv.stop,
                    qty=pos.qty, risk_usd=meta['risk_usd'], pnl=pnl,
                    r_multiple=pnl / meta['risk_usd'] if meta['risk_usd'] else 0.0, exit_kind=kind,
                    trigger=meta['trigger'], pullback_ok=meta['pullback_ok'], rs_ok=meta['rs_ok'],
                    score=meta['score']))
                del open_pos[pair]
                loss_streak = loss_streak + 1 if pnl < 0 else 0
                if use_guard and loss_streak >= cfg.MAX_CONSECUTIVE_LOSSES:
                    pause_until = t + int(cfg.LOSS_STREAK_COOLDOWN_HOURS * 3_600_000)
                dk = t // 86_400_000
                if dk != day_key:
                    day_key, day_pnl = dk, 0.0
                day_pnl += pnl

        # кривая капитала (по закрытым сделкам + текущая оценка открытых)
        mark = equity
        for pair, (pos, _) in open_pos.items():
            last = series[pair]["15"].upto(t, 1)
            if last:
                mark += (last[-1]['close'] - pos.entry) * pos.remaining
        res.equity_curve.append((t, mark))
        peak = max(peak, mark)
        if use_guard and res.halted_at is None and peak > 0 and 1 - mark / peak >= cfg.MAX_DRAWDOWN_STOP:
            res.halted_at = t
        if res.halted_at is not None:
            continue
        if use_guard and t < pause_until:
            continue
        dk = t // 86_400_000
        if dk == day_key and day_pnl < 0 and -day_pnl >= equity * cfg.MAX_DAILY_LOSS:
            continue

        # ---------------- светофор (пересчёт раз в 4 часа)
        if use_regime:
            k = ('BTC', 'regime', btc["240"].index(t))
            if k not in cache:
                p, e, r, a = regime_metrics(btc["240"].upto(t, 260))
                cache[k] = (regime_allowed(p, e, r, a), a)
            allowed, _atr_ratio = cache[k]
            if not allowed:
                continue

        # ---------------- поиск входов
        active = len(open_pos)
        if active >= min(cfg.MAX_OPEN_POSITIONS, cfg.MAX_ACTIVE_PAIRS):
            continue
        open_risk = sum(m['risk_usd'] for _, m in open_pos.values())
        btc_1h = btc["60"].upto(t, 25)
        btc_change = (btc_1h[-1]['close'] / btc_1h[0]['close'] - 1) * 100 if len(btc_1h) >= 25 else 0.0
        btc_4h = btc["240"].upto(t, 120)

        candidates = []
        for pair in pairs:
            if pair in open_pos:
                continue
            s = series[pair]
            k4 = (pair, 'trend', s["240"].index(t))
            if k4 not in cache:
                c4 = s["240"].upto(t, 120)
                ok, _ = F.check_trend_4h(c4)
                cache[k4] = (ok, c4)
            trend_ok, c4 = cache[k4]
            if not trend_ok:
                continue
            k1 = (pair, 'pullback', s["60"].index(t))
            if k1 not in cache:
                c1 = s["60"].upto(t, 200)
                pb_ok, pb = F.check_pullback_1h(c1)
                cache[k1] = (pb_ok, pb, c1)
            pb_ok, pb, c1 = cache[k1]
            if 'atr_1h' not in pb:
                continue
            c15 = s["15"].upto(t, 60)
            if not c15 or c15[-1]['start'] + M15 != t:
                continue
            price = c15[-1]['close']
            trig_ok, trig = F.check_trigger_15m(c15, price)
            if not trig_ok:
                continue
            res.signals_seen += 1
            day_ago = c1[-25]['close'] if len(c1) >= 25 else c1[0]['close']
            coin_change = (price / day_ago - 1) * 100
            rs_ok, _ = F.relative_strength(c4, btc_4h, coin_change, btc_change)
            if require_pullback and not pb_ok:
                skip('нет отката 1h')
                continue
            if require_rs and not rs_ok:
                skip('слабее BTC')
                continue
            fs = F.score_candidate({'trend_ok': True, 'pullback_ok': pb_ok, 'trigger_ok': True, 'rs_ok': rs_ok})
            plan, why = F.build_trade_plan(price, pb['pullback_low'], pb['atr_1h'])
            if plan is None:
                if why.startswith('комиссии'):
                    skip(f'комиссии > {cfg.FUNNEL_MAX_FEE_SHARE * 100:.0f}% риска (стоп близко)')
                elif why.startswith('чистый R/R'):
                    skip(f'чистый R/R < {cfg.FUNNEL_MIN_NET_RR}')
                else:
                    skip(why.split(' (')[0][:40])
                continue
            candidates.append((fs.score, pair, plan, trig, pb_ok, rs_ok, c1))

        candidates.sort(key=lambda x: -x[0])
        for score, pair, plan, trig, pb_ok, rs_ok, c1 in candidates:
            if len(open_pos) >= min(cfg.MAX_OPEN_POSITIONS, cfg.MAX_ACTIVE_PAIRS):
                break
            # корреляция с открытыми позициями
            closes = [c['close'] for c in c1][-168:]
            if any((F.returns_correlation(closes, [c['close'] for c in series[o]["60"].upto(t, 200)][-168:]) or 0)
                   > cfg.FUNNEL_CORR_MAX for o in open_pos):
                skip('корреляция')
                continue
            risk_frac = cfg.MAX_RISK_PER_TRADE
            if open_risk + equity * risk_frac > equity * cfg.MAX_TOTAL_RISK:
                skip('суммарный риск')
                continue
            qty = X.position_size(equity, risk_frac, plan.entry, plan.stop)
            if qty * plan.entry < cfg.MIN_ORDER_SIZE_USD:
                skip('позиция мала')
                continue
            qty = min(qty, equity * 0.98 / plan.entry)
            risk_usd = qty * (plan.entry - plan.stop + X.cost_per_unit(plan.entry))
            lv = X.levels(plan.entry, plan.stop, (plan.take_profit - plan.entry) / plan.risk_per_unit)
            pos = X.SimPosition(pair=pair, entry=plan.entry, qty=qty, lv=lv, atr=plan.atr, opened_ts=t)
            open_pos[pair] = (pos, {'risk_usd': risk_usd, 'trigger': trig.get('trigger', ''),
                                    'pullback_ok': pb_ok, 'rs_ok': rs_ok, 'score': score})
            open_risk += risk_usd

    # незакрытые к концу периода — по последней цене
    for pair, (pos, meta) in open_pos.items():
        last = series[pair]["15"].upto(end_ts, 1)
        if last:
            pos.fills.append(('end', last[-1]['close'], pos.remaining))
            pos.remaining = 0
            pnl = X.realized_pnl(pos)
            equity += pnl
            res.trades.append(Trade(pair=pair, opened_ts=pos.opened_ts, closed_ts=end_ts, entry=pos.entry,
                                    stop=pos.lv.stop, qty=pos.qty, risk_usd=meta['risk_usd'], pnl=pnl,
                                    r_multiple=pnl / meta['risk_usd'] if meta['risk_usd'] else 0.0,
                                    exit_kind='end', trigger=meta['trigger'], pullback_ok=meta['pullback_ok'],
                                    rs_ok=meta['rs_ok'], score=meta['score']))
    return res


# ----------------------------------------------------------------- метрики

def metrics(res: Result) -> Dict:
    tr = res.trades
    n = len(tr)
    wins = [t for t in tr if t.pnl > 0]
    losses = [t for t in tr if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    curve = [e for _, e in res.equity_curve] or [res.start_equity]
    peak, max_dd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, 1 - e / peak)
    final = res.start_equity + sum(t.pnl for t in tr)
    days = ((res.equity_curve[-1][0] - res.equity_curve[0][0]) / 86_400_000) if len(res.equity_curve) > 1 else 0
    # дневные доходности для Шарпа
    daily: Dict[int, float] = {}
    for ts, e in res.equity_curve:
        daily[ts // 86_400_000] = e
    vals = [daily[k] for k in sorted(daily)]
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1] > 0]
    sharpe = 0.0
    if len(rets) > 2:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = mean / sd * math.sqrt(365) if sd > 0 else 0.0
    by_exit: Dict[str, int] = {}
    for t in tr:
        by_exit[t.exit_kind] = by_exit.get(t.exit_kind, 0) + 1
    return {
        'trades': n,
        'win_rate': len(wins) / n if n else 0.0,
        'avg_r': sum(t.r_multiple for t in tr) / n if n else 0.0,
        'expectancy_usd': sum(t.pnl for t in tr) / n if n else 0.0,
        'profit_factor': gross_win / gross_loss if gross_loss > 0 else (float('inf') if gross_win > 0 else 0.0),
        'total_return': final / res.start_equity - 1 if res.start_equity else 0.0,
        'max_drawdown': max_dd,
        'sharpe': sharpe,
        'days': days,
        'trades_per_week': n / (days / 7) if days > 0 else 0.0,
        'by_exit': by_exit,
        'halted': res.halted_at is not None,
        'signals_seen': res.signals_seen,
        'skipped': dict(sorted(res.skipped.items(), key=lambda kv: -kv[1])),
    }


def breakdown(trades: List[Trade], key) -> Dict[str, Dict]:
    """Средний R и число сделок по группам (например, был ли откат / сила к BTC)."""
    groups: Dict[str, List[Trade]] = {}
    for t in trades:
        groups.setdefault(str(key(t)), []).append(t)
    return {g: {'trades': len(v), 'avg_r': sum(x.r_multiple for x in v) / len(v),
                'win_rate': sum(1 for x in v if x.pnl > 0) / len(v)} for g, v in groups.items()}


# ----------------------------------------------------------------- walk-forward

DEFAULT_GRID = [
    {'FUNNEL_TP_R': tp, 'EXIT_PARTIAL_ENABLED': part}
    for tp in (2.0, 2.5, 3.0) for part in (True, False)
]


def walk_forward(data, pairs, *, start_ts: int, end_ts: int, folds: int = 4, grid: Optional[List[Dict]] = None,
                 min_train_trades: int = 15, equity: float = 10_000.0, log=print, runner=None, **run_kw) -> Dict:
    """
    Делим период на folds+1 равных отрезков. Для каждого шага: подбираем параметры
    из сетки на всех предыдущих отрезках (train), проверяем на следующем (test).
    Итог — только по test-отрезкам: это и есть оценка «на невиданных данных».
    """
    grid = grid or DEFAULT_GRID
    runner = runner or run
    seg = (end_ts - start_ts) // (folds + 1)
    oos_trades: List[Trade] = []
    steps = []
    for k in range(1, folds + 1):
        tr_lo, tr_hi = start_ts, start_ts + seg * k
        te_lo, te_hi = tr_hi, min(end_ts, tr_hi + seg)
        best, best_score, best_m = None, -1e9, None
        for params in grid:
            with override(params):
                m = metrics(runner(data, pairs, start_ts=tr_lo, end_ts=tr_hi, equity=equity, **run_kw))
            # критерий: средний R при достаточном числе сделок (устойчивее профит-фактора)
            score = m['avg_r'] if m['trades'] >= min_train_trades else -1e6 + m['trades']
            if score > best_score:
                best, best_score, best_m = params, score, m
        with override(best):
            test_res = runner(data, pairs, start_ts=te_lo, end_ts=te_hi, equity=equity, **run_kw)
        tm = metrics(test_res)
        oos_trades.extend(test_res.trades)
        steps.append({'train': (tr_lo, tr_hi), 'test': (te_lo, te_hi), 'params': best,
                      'train_metrics': best_m, 'test_metrics': tm})
        log(f"  шаг {k}/{folds}: лучшие {best} | train avgR {best_m['avg_r']:+.2f} ({best_m['trades']}) "
            f"→ test avgR {tm['avg_r']:+.2f} ({tm['trades']} сделок)")
    n = len(oos_trades)
    return {
        'steps': steps,
        'oos_trades': n,
        'oos_avg_r': sum(t.r_multiple for t in oos_trades) / n if n else 0.0,
        'oos_win_rate': sum(1 for t in oos_trades if t.pnl > 0) / n if n else 0.0,
        'oos_profit_factor': (sum(t.pnl for t in oos_trades if t.pnl > 0) /
                              max(1e-9, -sum(t.pnl for t in oos_trades if t.pnl <= 0))) if n else 0.0,
    }


# ----------------------------------------------------------------- трендовая стратегия (D1)

TREND_GRID = [
    {'TREND_BREAKOUT_DAYS': b, 'TREND_TRAIL_ATR': t}
    for b in (20, 55) for t in (2.5, 3.0, 4.0)
]


def run_trend(data: Dict[str, Dict[str, List[dict]]], pairs: Iterable[str], *,
              start_ts: int, end_ts: int, equity: float = 10_000.0, use_guard: bool = True,
              ext: Optional[Dict[str, Dict[int, dict]]] = None, **_ignored) -> Result:
    """
    Трендовая стратегия на дневных свечах (склеенных из 4h). Вход по закрытию дня, стоп внутри дня.
    ext — признаки других бирж {pair: {день: {...}}}; фильтры включаются флагами TREND_EXT_*.
    """
    from bot.services import trend_strategy as T
    from bot.services.ext_market_data import ext_filter, any_filter_enabled
    use_ext = bool(ext) and any_filter_enabled()
    slip = cfg.SLIPPAGE_ESTIMATE
    D1 = T.D1
    pairs = [p for p in pairs if p in data]
    days = {p: T.daily_from_4h(data[p]['240']) for p in pairs}
    res = Result(start_equity=equity)
    open_pos: Dict[str, dict] = {}
    timeline = sorted({d['start'] + D1 for p in pairs for d in days[p] if start_ts <= d['start'] + D1 <= end_ts})
    peak = equity

    def close(pair, pos, price, t, kind):
        nonlocal equity
        fee = cfg.TRADING_FEE_RATE
        pnl = price * pos['qty'] * (1 - fee) - pos['entry'] * pos['qty'] * (1 + fee)
        equity += pnl
        res.trades.append(Trade(pair=pair, opened_ts=pos['opened'], closed_ts=t, entry=pos['entry'],
                                stop=pos['initial_stop'], qty=pos['qty'], risk_usd=pos['risk_usd'], pnl=pnl,
                                r_multiple=pnl / pos['risk_usd'] if pos['risk_usd'] else 0.0, exit_kind=kind,
                                trigger='breakout', pullback_ok=False, rs_ok=False, score=0))

    for t in timeline:
        for pair in pairs:
            hist = [d for d in days[pair] if d['start'] + D1 <= t]
            if not hist or hist[-1]['start'] + D1 != t:
                continue
            today = hist[-1]
            pos = open_pos.get(pair)
            if pos:
                if today['low'] <= pos['stop']:
                    close(pair, pos, min(pos['stop'], today['open']) * (1 - slip), t, 'trail_stop'
                          if pos['stop'] > pos['initial_stop'] else 'stop')
                    del open_pos[pair]
                else:
                    pos['highest'] = max(pos['highest'], today['close'])
                    atr = T.daily_atr(hist[-60:]) or pos['atr']
                    pos['stop'] = T.chandelier_stop(pos['stop'], pos['highest'], atr)
                continue
            if res.halted_at is not None or len(open_pos) >= cfg.TREND_MAX_POSITIONS:
                continue
            ok, info = T.entry_signal(hist[-(cfg.TREND_FILTER_EMA + 60):])
            if not ok:
                continue
            res.signals_seen += 1
            if use_ext:
                ok_ext, why = ext_filter((ext.get(pair) or {}).get(today['start']))
                if not ok_ext:
                    key = ('funding высокий' if why.startswith('funding') else
                           'объём пробоя слабый' if why.startswith('объём') else 'премия Coinbase низкая')
                    res.skipped[key] = res.skipped.get(key, 0) + 1
                    continue
            entry, stop = today['close'] * (1 + slip), info['stop']
            qty = X.position_size(equity, cfg.TREND_RISK_PER_TRADE, entry, stop)
            qty = min(qty, equity * 0.98 / entry)
            if qty * entry < cfg.MIN_ORDER_SIZE_USD:
                continue
            open_pos[pair] = {'entry': entry, 'stop': stop, 'initial_stop': stop, 'qty': qty, 'opened': t,
                              'highest': today['close'], 'atr': info['atr'],
                              'risk_usd': qty * (entry - stop + X.cost_per_unit(entry))}
        mark = equity + sum((_last_close(days[p], t) - pos['entry']) * pos['qty'] for p, pos in open_pos.items())
        res.equity_curve.append((t, mark))
        peak = max(peak, mark)
        if use_guard and res.halted_at is None and peak > 0 and 1 - mark / peak >= cfg.MAX_DRAWDOWN_STOP:
            res.halted_at = t

    for pair, pos in open_pos.items():
        close(pair, pos, _last_close(days[pair], end_ts), end_ts, 'end')
    return res


def _last_close(days: List[dict], t: int) -> float:
    i = bisect.bisect_right([d['start'] + 86_400_000 for d in days], t)
    return days[max(0, i - 1)]['close'] if days else 0.0
