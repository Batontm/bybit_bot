"""
Repository для работы со сделками, ордерами и позициями
"""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from config.settings import logger, TIMEZONE
from .connection import get_db, get_transaction


class TradesRepository:
    """Управление сделками и позициями"""
    
    # ========== ORDERS ==========
    
    @staticmethod
    def create_order(order_data: Dict[str, Any]) -> int:
        """Создать новый ордер"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO orders (
                    order_id, pair, side, order_type, price, quantity,
                    filled_quantity, status, created_at, updated_at,
                    is_tp, is_sl, position_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_data['order_id'],
                order_data['pair'],
                order_data['side'],
                order_data['order_type'],
                order_data.get('price'),
                order_data['quantity'],
                order_data.get('filled_quantity', 0),
                order_data['status'],
                order_data['created_at'],
                order_data['updated_at'],
                order_data.get('is_tp', False),
                order_data.get('is_sl', False),
                order_data.get('position_id')
            ))
            
            order_id = cursor.lastrowid
            logger.info(f"✅ Ордер создан: {order_data['order_id']} | {order_data['pair']} | {order_data['side']}")
            return order_id
    
    @staticmethod
    def update_order_status(order_id: str, status: str, filled_qty: float = None, 
                           avg_price: float = None) -> bool:
        """Обновить статус ордера"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            
            update_fields = ["status = ?", "updated_at = ?"]
            values = [status, datetime.now(TIMEZONE).isoformat()]
            
            if filled_qty is not None:
                update_fields.append("filled_quantity = ?")
                values.append(filled_qty)
            
            if avg_price is not None:
                update_fields.append("avg_fill_price = ?")
                values.append(avg_price)
            
            values.append(order_id)
            
            cursor.execute(f"""
                UPDATE orders 
                SET {', '.join(update_fields)}
                WHERE order_id = ?
            """, values)
            
            updated = cursor.rowcount > 0
            if updated:
                logger.info(f"📝 Ордер обновлён: {order_id} → {status}")
            return updated

    @staticmethod
    def update_position_pnl_breakdown(
        position_id: int,
        *,
        commission_paid: float,
        slippage: float,
        gross_pnl: float,
        net_pnl: float,
    ) -> bool:
        """Сохранить детализацию PnL для позиции. Также синхронизирует realized_pnl с net_pnl."""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE positions
                SET commission_paid = ?,
                    slippage = ?,
                    gross_pnl = ?,
                    net_pnl = ?,
                    realized_pnl = ?
                WHERE id = ?
                """,
                (commission_paid, slippage, gross_pnl, net_pnl, net_pnl, position_id),
            )
            return cursor.rowcount > 0

    @staticmethod
    def update_position_open_costs(
        position_id: int,
        *,
        commission_paid: float,
        slippage: float,
    ) -> bool:
        """Обновить накопленные издержки по открытой позиции (без изменения PnL полей)."""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE positions
                SET commission_paid = ?,
                    slippage = ?
                WHERE id = ?
                """,
                (commission_paid, slippage, position_id),
            )
            return cursor.rowcount > 0
    
    @staticmethod
    def get_order_by_id(order_id: str) -> Optional[Dict]:
        """Получить ордер по ID"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    @staticmethod
    def get_open_orders(pair: str = None) -> List[Dict]:
        """Получить открытые ордера"""
        with get_db() as conn:
            cursor = conn.cursor()
            
            if pair:
                cursor.execute("""
                    SELECT * FROM orders 
                    WHERE status IN ('New', 'PartiallyFilled') AND pair = ?
                    ORDER BY created_at DESC
                """, (pair,))
            else:
                cursor.execute("""
                    SELECT * FROM orders 
                    WHERE status IN ('New', 'PartiallyFilled')
                    ORDER BY created_at DESC
                """)
            
            return [dict(row) for row in cursor.fetchall()]
    
    @staticmethod
    def get_sl_order(position_id: int) -> Optional[Dict]:
        """Получить активный SL ордер позиции"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM orders 
                WHERE position_id = ? AND is_sl = 1 AND status IN ('New', 'PartiallyFilled')
                ORDER BY created_at DESC
                LIMIT 1
            """, (position_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    def update_order_price(order_id: str, new_price: float) -> bool:
        """Обновить цену ордера в БД"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE orders 
                SET price = ?, updated_at = ?
                WHERE order_id = ?
            """, (new_price, datetime.now(TIMEZONE).isoformat(), order_id))
            return cursor.rowcount > 0

    @staticmethod
    def update_order_slippage(order_id: str, slippage: float) -> bool:
        """Сохранить slippage по ордеру (в процентах доли, например 0.002 = 0.2%)."""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE orders
                SET slippage = ?, updated_at = ?
                WHERE order_id = ?
            """, (slippage, datetime.now(TIMEZONE).isoformat(), order_id))
            return cursor.rowcount > 0

    @staticmethod
    def get_order_position_id(order_id: str) -> Optional[int]:
        """Получить position_id, привязанный к ордеру"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT position_id FROM orders WHERE order_id = ?", (order_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return row.get('position_id')

    # ========== POSITIONS ==========
    
    @staticmethod
    def create_position(position_data: Dict[str, Any]) -> int:
        """Создать новую позицию"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO positions (
                    pair, entry_price, quantity, current_price,
                    tp_price, sl_price, status, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position_data['pair'],
                position_data['entry_price'],
                position_data['quantity'],
                position_data.get('current_price', position_data['entry_price']),
                position_data.get('tp_price'),
                position_data.get('sl_price'),
                'OPEN',
                position_data.get('opened_at', datetime.now(TIMEZONE).isoformat())
            ))
            
            position_id = cursor.lastrowid
            logger.info(f"🟢 Позиция открыта: {position_data['pair']} | "
                       f"Цена: {position_data['entry_price']} | "
                       f"Кол-во: {position_data['quantity']}")
            return position_id
    
    @staticmethod
    def update_position_price(position_id: int, current_price: float) -> bool:
        """Обновить текущую цену и PnL позиции"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            
            # Получаем данные позиции
            cursor.execute("""
                SELECT entry_price, quantity FROM positions WHERE id = ?
            """, (position_id,))
            row = cursor.fetchone()
            
            if not row:
                return False
            
            entry_price = row['entry_price']
            quantity = row['quantity']
            
            # Расчёт PnL
            unrealized_pnl = (current_price - entry_price) * quantity
            unrealized_pnl_percent = ((current_price - entry_price) / entry_price) * 100
            
            cursor.execute("""
                UPDATE positions
                SET current_price = ?,
                    unrealized_pnl = ?,
                    unrealized_pnl_percent = ?
                WHERE id = ?
            """, (current_price, unrealized_pnl, unrealized_pnl_percent, position_id))
            
            return cursor.rowcount > 0
    
    @staticmethod
    def close_position(position_id: int, exit_price: float, realized_pnl: float) -> bool:
        """Закрыть позицию"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE positions
                SET status = 'CLOSED',
                    closed_at = ?,
                    current_price = ?,
                    realized_pnl = ?
                WHERE id = ?
            """, (datetime.now(TIMEZONE).isoformat(), exit_price, realized_pnl, position_id))
            
            updated = cursor.rowcount > 0
            if updated:
                logger.info(f"🔴 Позиция закрыта: ID={position_id} | PnL={realized_pnl:.2f}")
            return updated
    
    @staticmethod
    def get_open_positions(pair: str = None) -> List[Dict]:
        """Получить открытые позиции"""
        with get_db() as conn:
            cursor = conn.cursor()
            
            if pair:
                cursor.execute("""
                    SELECT * FROM positions 
                    WHERE status = 'OPEN' AND pair = ?
                    ORDER BY opened_at DESC
                """, (pair,))
            else:
                cursor.execute("""
                    SELECT * FROM positions 
                    WHERE status = 'OPEN'
                    ORDER BY opened_at DESC
                """)
            
            return [dict(row) for row in cursor.fetchall()]
    
    @staticmethod
    def get_position_by_id(position_id: int) -> Optional[Dict]:
        """Получить позицию по ID"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM positions WHERE id = ?", (position_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    @staticmethod
    def get_closed_positions_between_utc(start_utc: datetime, end_utc: datetime) -> List[Dict]:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM positions
                WHERE status = 'CLOSED' AND closed_at IS NOT NULL
                ORDER BY closed_at ASC
                """
            )
            rows = [dict(r) for r in cursor.fetchall()]

        result: List[Dict] = []
        for pos in rows:
            closed_at_raw = pos.get('closed_at')
            if not closed_at_raw:
                continue
            try:
                dt = datetime.fromisoformat(str(closed_at_raw))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=TIMEZONE)
                dt_utc = dt.astimezone(timezone.utc)
            except Exception:
                continue

            if start_utc <= dt_utc < end_utc:
                result.append(pos)

        return result
    
    @staticmethod
    def set_position_tpsl(position_id: int, tp_price: float = None, 
                         sl_price: float = None) -> bool:
        """Установить TP/SL для позиции"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            
            updates = []
            values = []
            
            if tp_price is not None:
                updates.append("tp_price = ?")
                values.append(tp_price)
            
            if sl_price is not None:
                updates.append("sl_price = ?")
                values.append(sl_price)
            
            if not updates:
                return False
            
            values.append(position_id)
            
            cursor.execute(f"""
                UPDATE positions SET {', '.join(updates)}
                WHERE id = ?
            """, values)
            
            return cursor.rowcount > 0
    
    @staticmethod
    def update_position_quantity(position_id: int, new_quantity: float) -> bool:
        """
        Обновить quantity позиции (синхронизация с реальным балансом).
        Также пересчитывает unrealized PnL.
        """
        with get_transaction() as conn:
            cursor = conn.cursor()
            
            # Получаем entry_price для пересчёта PnL
            cursor.execute("""
                SELECT entry_price, current_price FROM positions WHERE id = ?
            """, (position_id,))
            row = cursor.fetchone()
            
            if not row:
                return False
            
            entry_price = row['entry_price']
            current_price = row['current_price'] or entry_price
            
            # Пересчёт PnL
            unrealized_pnl = (current_price - entry_price) * new_quantity
            unrealized_pnl_percent = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
            
            cursor.execute("""
                UPDATE positions
                SET quantity = ?,
                    unrealized_pnl = ?,
                    unrealized_pnl_percent = ?
                WHERE id = ?
            """, (new_quantity, unrealized_pnl, unrealized_pnl_percent, position_id))
            
            updated = cursor.rowcount > 0
            if updated:
                logger.info(f"📊 Позиция #{position_id}: qty обновлено → {new_quantity:.6f}")
            return updated
    
    # ========== TRADES ==========
    
    @staticmethod
    def create_trade(trade_data: Dict[str, Any]) -> int:
        """Записать сделку (fill)"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trades (
                    trade_id, order_id, pair, side, price, quantity,
                    fee, fee_asset, executed_at, position_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data['trade_id'],
                trade_data['order_id'],
                trade_data['pair'],
                trade_data['side'],
                trade_data['price'],
                trade_data['quantity'],
                trade_data.get('fee', 0),
                trade_data.get('fee_asset', 'USDT'),
                trade_data['executed_at'],
                trade_data.get('position_id')
            ))
            
            trade_id = cursor.lastrowid
            logger.debug(f"📊 Сделка записана: {trade_data['trade_id']}")
            return trade_id

    @staticmethod
    def trade_exists(trade_id: str) -> bool:
        """Проверить, есть ли trade_id в БД"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM trades WHERE trade_id = ? LIMIT 1", (trade_id,))
            return cursor.fetchone() is not None

    @staticmethod
    def create_trade_if_not_exists(trade_data: Dict[str, Any]) -> bool:
        """Записать сделку (fill) если её ещё нет"""
        trade_id = trade_data.get('trade_id')
        if not trade_id:
            return False

        if TradesRepository.trade_exists(trade_id):
            return False

        TradesRepository.create_trade(trade_data)
        return True

    @staticmethod
    def get_trade_ids_for_order(order_id: str) -> set[str]:
        """Получить множество trade_id для order_id"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT trade_id FROM trades WHERE order_id = ?", (order_id,))
            return {row['trade_id'] for row in cursor.fetchall()}
    
    @staticmethod
    def get_trades_by_position(position_id: int) -> List[Dict]:
        """Получить все сделки по позиции"""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trades 
                WHERE position_id = ?
                ORDER BY executed_at ASC
            """, (position_id,))
            
            return [dict(row) for row in cursor.fetchall()]
