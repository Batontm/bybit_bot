"""
Repository для работы с PnL статистикой
"""
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
from config.settings import logger, TIMEZONE
from .connection import get_db, get_transaction


class PnLRepository:
    """Управление статистикой PnL"""
    
    @staticmethod
    def record_daily_pnl(date: str, pair: str, realized_pnl: float, 
                        is_win: bool) -> None:
        """Записать дневной PnL по паре"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            
            # Проверяем существование записи
            cursor.execute("""
                SELECT id, realized_pnl, trades_count, wins, losses
                FROM pnl_history WHERE date = ? AND pair = ?
            """, (date, pair))
            
            row = cursor.fetchone()
            
            if row:
                # Обновляем существующую запись
                new_pnl = row['realized_pnl'] + realized_pnl
                new_count = row['trades_count'] + 1
                new_wins = row['wins'] + (1 if is_win else 0)
                new_losses = row['losses'] + (0 if is_win else 1)
                
                cursor.execute("""
                    UPDATE pnl_history
                    SET realized_pnl = ?, trades_count = ?, wins = ?, losses = ?
                    WHERE date = ? AND pair = ?
                """, (new_pnl, new_count, new_wins, new_losses, date, pair))
            else:
                # Создаём новую запись
                cursor.execute("""
                    INSERT INTO pnl_history (date, pair, realized_pnl, trades_count, wins, losses)
                    VALUES (?, ?, ?, 1, ?, ?)
                """, (date, pair, realized_pnl, 1 if is_win else 0, 0 if is_win else 1))
            
            logger.debug(f"📈 PnL записан: {date} | {pair} | {realized_pnl:.2f}")
    
    @staticmethod
    def get_pnl_by_pairs(days: int = 7) -> List[Dict]:
        """Получить PnL по парам за последние N дней"""
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Вычисляем дату начала периода
            start_date = (datetime.now(TIMEZONE) - timedelta(days=days)).strftime("%Y-%m-%d")
            
            cursor.execute("""
                SELECT 
                    pair,
                    SUM(realized_pnl) as total_pnl,
                    SUM(trades_count) as total_trades,
                    SUM(wins) as total_wins,
                    SUM(losses) as total_losses
                FROM pnl_history
                WHERE date >= ?
                GROUP BY pair
                ORDER BY total_pnl DESC
            """, (start_date,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    @staticmethod
    def get_pnl_by_days(days: int = 7) -> List[Dict]:
        """Получить PnL по дням за последние N дней"""
        with get_db() as conn:
            cursor = conn.cursor()
            
            start_date = (datetime.now(TIMEZONE) - timedelta(days=days)).strftime("%Y-%m-%d")
            
            cursor.execute("""
                SELECT 
                    date,
                    SUM(realized_pnl) as daily_pnl,
                    SUM(trades_count) as daily_trades,
                    SUM(wins) as daily_wins,
                    SUM(losses) as daily_losses
                FROM pnl_history
                WHERE date >= ?
                GROUP BY date
                ORDER BY date DESC
            """, (start_date,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    @staticmethod
    def get_total_pnl(days: int = 7) -> Tuple[float, int]:
        """Получить общий PnL и количество сделок за период"""
        with get_db() as conn:
            cursor = conn.cursor()
            
            start_date = (datetime.now(TIMEZONE) - timedelta(days=days)).strftime("%Y-%m-%d")
            
            cursor.execute("""
                SELECT 
                    SUM(realized_pnl) as total_pnl,
                    SUM(trades_count) as total_trades
                FROM pnl_history
                WHERE date >= ?
            """, (start_date,))
            
            row = cursor.fetchone()
            return (
                row['total_pnl'] or 0.0,
                row['total_trades'] or 0
            )
    
    @staticmethod
    def get_current_day_pnl() -> float:
        """Получить PnL за текущий день"""
        with get_db() as conn:
            cursor = conn.cursor()
            
            today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            
            cursor.execute("""
                SELECT SUM(realized_pnl) as daily_pnl
                FROM pnl_history
                WHERE date = ?
            """, (today,))
            
            row = cursor.fetchone()
            return row['daily_pnl'] or 0.0
