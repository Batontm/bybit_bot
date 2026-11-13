"""
Бумажная торговля: сделки стратегий-кандидатов считаются без ордеров.

Раз в сутки (после закрытия дневной свечи) для каждой стратегии со ступенью
PAPER или LIVE и каждой пары из PAPER_PAIRS правила стратегии проигрываются на
свечах с момента запуска бумажного режима — теми же функциями, что в
лаборатории (bot/backtest/lab.py). Результат синхронизируется с таблицей
paper_trades: новые сделки добавляются, открытые обновляются, закрытые не
переписываются (журнал честный).

Стратегии LIVE тоже ведутся на бумаге («тень»): по расхождению тени и живых
сделок видно, совпадает ли исполнение с расчётом. Каждая сделка помечается
состоянием рынка на входе — так копится материал для запретов вида
«не покупать в падении».

Перевод с бумаги в торговлю — только вручную, после PAPER_MIN_TRADES сделок
с плюсом (config.trading_config.STRATEGY_ADMISSION).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import trading_config as cfg
from config.settings import logger
from ..db.connection import get_db, get_transaction

D1 = 86_400_000


def _setting(key: str) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row['value'] if row else None


def started_at(create: bool = False) -> Optional[int]:
    """Момент запуска бумажного режима (мс). Сделки считаются только после него."""
    v = _setting('paper_started_at')
    if v:
        return int(v)
    if not create:
        return None
    now = int(time.time() * 1000)
    with get_transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('paper_started_at', ?)", (str(now),))
    return now


def paper_stats(name: str) -> Dict:
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT status, r, state FROM paper_trades WHERE strategy = ? "
                                "AND status IN ('OPEN', 'CLOSED')", (name,)).fetchall()
    except Exception:
        rows = []
    closed = [float(r['r'] or 0) for r in rows if r['status'] == 'CLOSED']
    by_state: Dict[str, List[float]] = {}
    for r in rows:
        if r['status'] == 'CLOSED':
            by_state.setdefault(r['state'] or '—', []).append(float(r['r'] or 0))
    return {'closed': len(closed), 'open': sum(1 for r in rows if r['status'] == 'OPEN'),
            'avg_r': sum(closed) / len(closed) if closed else 0.0, 'sum_r': sum(closed),
            'by_state': {k: {'n': len(v), 'avg_r': sum(v) / len(v)} for k, v in by_state.items()}}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M')


def sync(name: str, title: str, pair: str, trades: list) -> List[str]:
    """Привести таблицу paper_trades в соответствие с проигранными сделками одной пары."""
    messages: List[str] = []
    seen = set()
    with get_transaction() as conn:
        for t in trades:
            seen.add(t.opened)
            row = conn.execute("SELECT id, status FROM paper_trades WHERE strategy = ? AND pair = ? AND opened_ts = ?",
                               (name, pair, t.opened)).fetchone()
            is_open = t.exit == 'end'
            side = "лонг" if t.side > 0 else "шорт"
            if row is None:
                conn.execute(
                    "INSERT INTO paper_trades (strategy, pair, side, opened_ts, opened_at, entry, stop, state, "
                    "status, closed_ts, closed_at, exit_price, r, exit_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, pair, t.side, t.opened, _iso(t.opened), t.entry, t.stop, t.state,
                     'OPEN' if is_open else 'CLOSED', None if is_open else t.closed,
                     None if is_open else _iso(t.closed), None if is_open else t.exit_price, t.r,
                     None if is_open else t.exit))
                if is_open:
                    messages.append(f"📝 Бумага · {title}: {side} {pair} @ {t.entry:.6g}, стоп {t.stop:.6g}")
                else:      # открылась и закрылась между двумя расчётами
                    messages.append(f"📝 Бумага · {title}: {side} {pair} @ {t.entry:.6g} → закрыта {t.r:+.2f}R ({t.exit})")
            elif row['status'] == 'OPEN':
                if is_open:
                    conn.execute("UPDATE paper_trades SET stop = ?, r = ? WHERE id = ?", (t.stop, t.r, row['id']))
                else:
                    conn.execute("UPDATE paper_trades SET status = 'CLOSED', closed_ts = ?, closed_at = ?, "
                                 "exit_price = ?, r = ?, exit_kind = ?, stop = ? WHERE id = ?",
                                 (t.closed, _iso(t.closed), t.exit_price, t.r, t.exit, t.stop, row['id']))
                    messages.append(f"📝 Бумага · {title}: {pair} закрыта {t.r:+.2f}R ({t.exit})")
        # открытая запись, которой больше нет в расчёте (свечи биржи исправлены задним числом)
        for row in conn.execute("SELECT id, opened_ts FROM paper_trades WHERE strategy = ? AND pair = ? "
                                "AND status = 'OPEN'", (name, pair)).fetchall():
            if row['opened_ts'] not in seen:
                conn.execute("UPDATE paper_trades SET status = 'VOID' WHERE id = ?", (row['id'],))
    return messages


def run(fetch_daily=None) -> List[str]:
    """Проиграть правила на свежих дневных свечах. fetch_daily(pair) -> закрытые дневные свечи."""
    from . import admission as A
    from . import market_state as MS
    from bot.backtest import lab as L

    table = getattr(cfg, 'STRATEGY_ADMISSION', {}) or {}
    specs = {s.key: s for s in L.SPECS}
    active = [(n, e) for n, e in table.items()
              if e.get('spec') in specs and A.level(n) in (A.LIVE, A.PAPER)]
    if not active:
        return []
    if fetch_daily is None:
        from .trend_strategy import trend_service
        fetch_daily = lambda pair: trend_service._daily(pair, limit=1000)

    start = started_at(create=True)
    btc = fetch_daily('BTCUSDT')
    if not btc:
        return ["⚠️ Бумажная торговля: нет дневных свечей BTCUSDT"]
    lookup = MS.StateLookup(btc)
    messages: List[str] = []
    for pair in getattr(cfg, 'PAPER_PAIRS', []) or []:
        try:
            days = btc if pair == 'BTCUSDT' else fetch_daily(pair)
        except Exception as e:
            logger.warning(f"⚠️ Бумага {pair}: нет свечей ({e})")
            continue
        if not days or len(days) < 150:
            continue
        ind = L.indicators(days)
        for name, entry in active:
            trades = L.simulate(specs[entry['spec']], pair, days, ind, D1, start, 10 ** 18, lookup.at)
            msgs = sync(name, entry.get('title', name), pair, trades)
            if A.level(name) == A.PAPER:          # о «тени» живой стратегии не шумим
                messages += msgs
    return messages
