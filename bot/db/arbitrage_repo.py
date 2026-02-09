"""
Репозиторий для арбитражных позиций (Спот-Фьючерс)
"""
from typing import Dict, List, Optional
from datetime import datetime
from .connection import get_db, get_transaction
from config.settings import logger, TIMEZONE


class ArbitrageRepository:
    """Работа с арбитражными позициями в БД"""
    
    @staticmethod
    def init_table() -> None:
        """Создать таблицу arbitrage_positions если не существует"""
        with get_transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS arbitrage_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    spot_order_id TEXT,
                    futures_order_id TEXT,
                    spot_qty REAL NOT NULL,
                    futures_qty REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    accumulated_funding REAL DEFAULT 0.0,
                    funding_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'OPEN',
                    realized_pnl REAL DEFAULT 0.0,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    notes TEXT
                )
            """)
            logger.info("✅ Таблица arbitrage_positions готова")
    
    @staticmethod
    def create_position(data: Dict) -> int:
        """Создать новую арбитражную позицию"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO arbitrage_positions 
                (pair, spot_order_id, futures_order_id, spot_qty, futures_qty,
                 entry_price, accumulated_funding, status, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, 0.0, 'OPEN', ?)
            """, (
                data['pair'],
                data.get('spot_order_id'),
                data.get('futures_order_id'),
                data['spot_qty'],
                data['futures_qty'],
                data['entry_price'],
                datetime.now(TIMEZONE).isoformat()
            ))
            position_id = cursor.lastrowid
            logger.info(f"📊 Арбитраж создан: {data['pair']} | ID: {position_id}")
            return position_id
    
    @staticmethod
    def get_open_positions() -> List[Dict]:
        """Получить все открытые арбитражные позиции"""
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT * FROM arbitrage_positions 
                WHERE status = 'OPEN'
                ORDER BY opened_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
    
    @staticmethod
    def get_position_by_id(position_id: int) -> Optional[Dict]:
        """Получить позицию по ID"""
        with get_db() as conn:
            cursor = conn.execute(
                "SELECT * FROM arbitrage_positions WHERE id = ?",
                (position_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    @staticmethod
    def get_position_by_pair(pair: str) -> Optional[Dict]:
        """Получить открытую позицию по паре"""
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT * FROM arbitrage_positions 
                WHERE pair = ? AND status = 'OPEN'
            """, (pair,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    @staticmethod
    def update_funding(position_id: int, funding_amount: float) -> None:
        """Добавить funding к накопленному"""
        with get_transaction() as conn:
            conn.execute("""
                UPDATE arbitrage_positions 
                SET accumulated_funding = accumulated_funding + ?,
                    funding_count = funding_count + 1
                WHERE id = ?
            """, (funding_amount, position_id))
            logger.debug(f"💰 Funding обновлён: +{funding_amount:.4f} для позиции #{position_id}")
    
    @staticmethod
    def close_position(position_id: int, realized_pnl: float) -> None:
        """Закрыть арбитражную позицию"""
        with get_transaction() as conn:
            conn.execute("""
                UPDATE arbitrage_positions 
                SET status = 'CLOSED',
                    realized_pnl = ?,
                    closed_at = ?
                WHERE id = ?
            """, (
                realized_pnl,
                datetime.now(TIMEZONE).isoformat(),
                position_id
            ))
            logger.info(f"✅ Арбитраж закрыт: #{position_id} | PnL: {realized_pnl:.2f}")
    
    @staticmethod
    def get_total_accumulated_funding() -> float:
        """Получить общий накопленный funding по всем открытым позициям"""
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT COALESCE(SUM(accumulated_funding), 0) as total
                FROM arbitrage_positions 
                WHERE status = 'OPEN'
            """)
            row = cursor.fetchone()
            return row['total'] if row else 0.0
    
    @staticmethod
    def get_statistics() -> Dict:
        """Получить статистику по арбитражу"""
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT 
                    COUNT(*) as total_positions,
                    SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) as open_positions,
                    SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) as closed_positions,
                    COALESCE(SUM(CASE WHEN status = 'OPEN' THEN accumulated_funding ELSE 0 END), 0) as open_funding,
                    COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN realized_pnl ELSE 0 END), 0) as total_pnl
                FROM arbitrage_positions
            """)
            row = cursor.fetchone()
            return dict(row) if row else {}


# Глобальный экземпляр
arbitrage_repo = ArbitrageRepository()
