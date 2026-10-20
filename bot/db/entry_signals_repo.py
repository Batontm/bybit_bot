"""
Лог воронки входа: какие пары дошли до какого шага и с какими баллами.

Нужен, чтобы через пару недель на демо посчитать, какие компоненты оценки
реально связаны с прибыльными сделками (JOIN с positions по position_id).
"""
import json
from datetime import datetime
from typing import Dict, List, Optional

from config.settings import logger, TIMEZONE
from .connection import get_transaction, get_db


class EntrySignalsRepository:

    @staticmethod
    def log(pair: str, stage: str, *, score: Optional[int] = None,
            components: Optional[Dict] = None, metrics: Optional[Dict] = None,
            vetoes: Optional[List[str]] = None, decision: str = "SKIP",
            reason: str = "", price: Optional[float] = None,
            stop_price: Optional[float] = None, tp_price: Optional[float] = None) -> Optional[int]:
        try:
            with get_transaction() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO entry_signals (created_at, pair, stage, score, components, metrics,
                                               vetoes, decision, reason, price, stop_price, tp_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    datetime.now(TIMEZONE).isoformat(), pair, stage, score,
                    json.dumps(components or {}, ensure_ascii=False),
                    json.dumps(_jsonable(metrics or {}), ensure_ascii=False),
                    json.dumps(vetoes or [], ensure_ascii=False),
                    decision, reason[:500], price, stop_price, tp_price,
                ))
                return cur.lastrowid
        except Exception as e:
            logger.debug(f"entry_signals log error: {e}")
            return None

    @staticmethod
    def update(signal_id: int, *, decision: Optional[str] = None, reason: Optional[str] = None,
               position_id: Optional[int] = None) -> None:
        if not signal_id:
            return
        sets, args = [], []
        if decision is not None:
            sets.append("decision = ?")
            args.append(decision)
        if reason is not None:
            sets.append("reason = ?")
            args.append(reason[:500])
        if position_id is not None:
            sets.append("position_id = ?")
            args.append(position_id)
        if not sets:
            return
        try:
            with get_transaction() as conn:
                conn.execute(f"UPDATE entry_signals SET {', '.join(sets)} WHERE id = ?", (*args, signal_id))
        except Exception as e:
            logger.debug(f"entry_signals update error: {e}")

    @staticmethod
    def recent(limit: int = 20) -> List[Dict]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM entry_signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]


def _jsonable(d: Dict) -> Dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, float):
            out[k] = round(v, 6)
        elif isinstance(v, (int, str, bool)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out
