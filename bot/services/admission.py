"""
Допуск стратегий к торговле: запрещено всё, что не разрешено явно.

Принцип: не «в каждом состоянии рынка что-то должно торговать», а «торгует только
то, чьё преимущество достаточно убедительно подтверждено». Если разрешённых
стратегий нет — бот стоит в деньгах.

Три ступени (config.trading_config.STRATEGY_ADMISSION):
  OFF   — запрещена: проверку на истории не прошла (или её нет в таблице);
  PAPER — бумажная торговля: на истории выглядит хорошо, но доказательств мало.
          Сделки считаются без ордеров (bot/services/paper_service.py);
  LIVE  — разрешена: открывает реальные сделки.

Допуск LIVE отзывается автоматически (стратегия переходит на PAPER), если её
живой результат падает от своего максимума на ADMISSION_REVOKE_DD_R и больше —
это заметно хуже худшего периода на истории. Вернуть допуск может только человек.

Критерий «достаточно убедительно» для истории — evaluate(): его же использует
лаборатория (python -m bot.backtest.lab).
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from config import trading_config as cfg
from config.settings import TIMEZONE, logger
from ..db.connection import get_db, get_transaction

LIVE, PAPER, OFF = "LIVE", "PAPER", "OFF"
LEVEL_LABELS = {LIVE: "торгует", PAPER: "бумага", OFF: "запрещена"}
LEVEL_ICONS = {LIVE: "✅", PAPER: "📝", OFF: "⛔"}


# ----------------------------------------------------------------- таблица допуска

def _table() -> Dict[str, Dict]:
    return getattr(cfg, 'STRATEGY_ADMISSION', {}) or {}


def configured_level(name: str) -> str:
    entry = _table().get(name)
    level = (entry or {}).get('level', OFF)
    return level if level in (LIVE, PAPER, OFF) else OFF


def _setting(key: str) -> Optional[str]:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row['value'] if row else None
    except Exception:
        return None


def revoked(name: str) -> Optional[str]:
    """Причина отзыва допуска (или None)."""
    return _setting(f"admission_revoked_{name}") or None


def level(name: str) -> str:
    """Действующая ступень: неизвестная стратегия — OFF; отозванный LIVE — PAPER."""
    lv = configured_level(name)
    if lv == LIVE and revoked(name):
        return PAPER
    return lv


def is_live(name: str) -> bool:
    return level(name) == LIVE


def deny_reason(name: str) -> str:
    lv = level(name)
    if lv == LIVE:
        return ""
    why = revoked(name)
    if why:
        return f"допуск стратегии {name} отозван: {why}"
    if name not in _table():
        return f"стратегии {name} нет в таблице допуска"
    return f"стратегия {name} не допущена к торговле ({LEVEL_LABELS[lv]})"


# ----------------------------------------------------------------- критерий для истории

def bootstrap_p_nonpositive(rs: Sequence[float], rounds: int = 4000, seed: int = 1) -> float:
    """Доля перемешанных выборок, где средний результат не больше нуля."""
    n = len(rs)
    if n == 0:
        return 1.0
    rnd = random.Random(seed)
    bad = 0
    for _ in range(rounds):
        if sum(rs[rnd.randrange(n)] for _ in range(n)) <= 0:
            bad += 1
    return bad / rounds


def evaluate(trades: Sequence[Tuple[int, float]], mid_ts: int) -> Dict:
    """
    Строгая проверка: trades — пары (время входа, результат в R).
    Допуск: сделок ≥ ADMISSION_MIN_TRADES, средний > 0, вероятность случайного плюса
    < ADMISSION_MAX_P, плюс в обеих половинах периода.
    """
    rs = [r for _, r in trades]
    n = len(rs)
    avg = sum(rs) / n if n else 0.0
    first = [r for t, r in trades if t < mid_ts]
    second = [r for t, r in trades if t >= mid_ts]
    h1 = sum(first) / len(first) if first else 0.0
    h2 = sum(second) / len(second) if second else 0.0
    p = bootstrap_p_nonpositive(rs) if n else 1.0
    min_n = getattr(cfg, 'ADMISSION_MIN_TRADES', 100)
    max_p = getattr(cfg, 'ADMISSION_MAX_P', 0.05)
    out = {'n': n, 'avg_r': avg, 'p': p, 'h1': h1, 'h2': h2, 'verdict': OFF, 'why': ''}
    if n == 0:
        out['why'] = "нет сделок"
    elif avg <= 0:
        out['why'] = "в минусе"
    elif p >= max_p:
        out['why'] = f"плюс может быть случайным (вероятность {p * 100:.0f}%)"
    elif h1 <= 0 or h2 <= 0:
        out['why'] = f"нестабильно по времени: {h1:+.2f}R → {h2:+.2f}R"
    elif n < min_n:
        out['verdict'], out['why'] = PAPER, f"выглядит хорошо, но сделок {n} из {min_n} — на бумагу"
    else:
        out['verdict'], out['why'] = LIVE, "все условия выполнены"
    return out


# ----------------------------------------------------------------- живой результат и отзыв допуска

def live_r_series(name: str) -> List[float]:
    """Результаты закрытых живых сделок стратегии в R, от старых к новым."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT net_pnl, realized_pnl, risk_usd FROM positions "
                "WHERE status = 'CLOSED' AND strategy = ? AND risk_usd IS NOT NULL AND risk_usd > 0 "
                "ORDER BY closed_at", (name,)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        pnl = r['net_pnl'] if r['net_pnl'] not in (None, 0) else r['realized_pnl']
        if pnl is not None:
            out.append(float(pnl) / float(r['risk_usd']))
    return out


def r_drawdown(rs: Sequence[float]) -> float:
    """Текущая просадка накопленного результата от его максимума, в R."""
    total = peak = 0.0
    for r in rs:
        total += r
        peak = max(peak, total)
    return peak - total


def check_revocation() -> List[str]:
    """Отозвать допуск у стратегий LIVE, ушедших в глубокую просадку. Возвращает уведомления."""
    limit = float(getattr(cfg, 'ADMISSION_REVOKE_DD_R', 12.0))
    messages = []
    for name in _table():
        if configured_level(name) != LIVE or revoked(name):
            continue
        rs = live_r_series(name)
        dd = r_drawdown(rs)
        if dd >= limit:
            why = (f"просадка {dd:.1f}R от максимума за {len(rs)} сделок (порог {limit:.0f}R), "
                   f"{datetime.now(TIMEZONE).strftime('%d.%m.%Y')}")
            with get_transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                             (f"admission_revoked_{name}", why))
            logger.warning(f"⛔️ Допуск {name} отозван: {why}")
            messages.append(f"⛔️ Допуск стратегии {name} отозван: {why}. Новые входы остановлены, "
                            f"открытые позиции сопровождаются. Стратегия переведена на бумагу; "
                            f"вернуть допуск: /admission restore {name}")
    return messages


def restore(name: str) -> bool:
    """Вернуть отозванный допуск (только вручную)."""
    if not revoked(name):
        return False
    with get_transaction() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (f"admission_revoked_{name}",))
    logger.warning(f"✅ Допуск {name} возвращён вручную")
    return True


# ----------------------------------------------------------------- показ

def status() -> List[Dict]:
    """Строки для сводки и команды /admission."""
    from .paper_service import paper_stats
    rows = []
    for name, entry in _table().items():
        lv = level(name)
        row = {'name': name, 'title': entry.get('title', name), 'level': lv,
               'evidence': entry.get('evidence', ''), 'revoked': revoked(name)}
        if lv == LIVE:
            rs = live_r_series(name)
            row['live'] = {'n': len(rs), 'sum_r': sum(rs), 'drawdown_r': r_drawdown(rs)}
        if entry.get('spec') and lv in (LIVE, PAPER):
            row['paper'] = paper_stats(name)
        rows.append(row)
    return rows


def format_status(rows: List[Dict], *, detailed: bool = False) -> List[str]:
    need = int(getattr(cfg, 'PAPER_MIN_TRADES', 30))
    lines = []
    for r in rows:
        if r['level'] == OFF and not detailed:
            continue
        line = f"{LEVEL_ICONS[r['level']]} {r['title']} — {LEVEL_LABELS[r['level']]}"
        if r.get('revoked'):
            line += " (допуск отозван)"
        live = r.get('live')
        if live and live['n']:
            line += f"; живых сделок {live['n']}, итог {live['sum_r']:+.1f}R"
        paper = r.get('paper')
        if paper and r['level'] == PAPER:
            line += f"; на бумаге {paper['closed']} из {need} сделок"
            if paper['closed']:
                line += f", в среднем {paper['avg_r']:+.2f}R"
            if paper['open']:
                line += f", открыто {paper['open']}"
        lines.append(line)
        if detailed and r.get('evidence'):
            lines.append(f"   {r['evidence']}")
        if detailed and r.get('revoked'):
            lines.append(f"   отзыв: {r['revoked']}")
    off = [r['title'] for r in rows if r['level'] == OFF]
    if off and not detailed:
        lines.append(f"⛔ Запрещены: {', '.join(off)}")
    return lines
