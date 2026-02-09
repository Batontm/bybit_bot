"""
Repository для daily_pnl (UTC)
"""

from datetime import datetime
from typing import Optional, Dict, Any

from config.settings import TIMEZONE
from .connection import get_db, get_transaction


class DailyPnLRepository:
    @staticmethod
    def upsert_day(
        *,
        date_utc: str,
        gross_pnl: float,
        net_pnl: float,
        commission_paid: float,
        slippage: float,
        trades_count: int,
        wins: int,
        losses: int,
    ) -> None:
        with get_transaction() as conn:
            conn.execute(
                """
                INSERT INTO daily_pnl (
                    date_utc,
                    gross_pnl,
                    net_pnl,
                    commission_paid,
                    slippage,
                    trades_count,
                    wins,
                    losses,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date_utc) DO UPDATE SET
                    gross_pnl=excluded.gross_pnl,
                    net_pnl=excluded.net_pnl,
                    commission_paid=excluded.commission_paid,
                    slippage=excluded.slippage,
                    trades_count=excluded.trades_count,
                    wins=excluded.wins,
                    losses=excluded.losses,
                    updated_at=excluded.updated_at
                """,
                (
                    date_utc,
                    float(gross_pnl),
                    float(net_pnl),
                    float(commission_paid),
                    float(slippage),
                    int(trades_count),
                    int(wins),
                    int(losses),
                    datetime.now(TIMEZONE).isoformat(),
                ),
            )

    @staticmethod
    def get_day(date_utc: str) -> Optional[Dict[str, Any]]:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM daily_pnl WHERE date_utc = ?", (date_utc,))
            row = cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    def get_last_days(limit: int = 7) -> list[Dict[str, Any]]:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM daily_pnl ORDER BY date_utc DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in cursor.fetchall()]
