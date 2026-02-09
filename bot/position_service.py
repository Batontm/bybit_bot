"""
Сервис управления открытыми позициями (закрытие, TP/SL, Trailing Stop)
"""
from typing import List, Optional
from datetime import datetime, timedelta
import math
from pybit.unified_trading import HTTP
from config.settings import logger, TIMEZONE
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET
from config.trading_config import (
    TRAILING_ACTIVATION, TRAILING_STEP,
    BREAKEVEN_ENABLED, BREAKEVEN_TRIGGER_PERCENT, BREAKEVEN_BUFFER,
    TIME_EXIT_ENABLED, MAX_POSITION_HOURS, STALE_MOVE_THRESHOLD
)

from ..db.trades_repo import TradesRepository
from ..db.pnl_repo import PnLRepository
from .websocket_service import websocket_service
from ..error_tracker import error_tracker



class PositionService:
    """Управление позициями: мониторинг, модификация, закрытие"""
    
    # Кэш точности для пар (количество десятичных знаков)
    QTY_PRECISION = {
        'BTCUSDT': 6,   # BTC: до 0.000001
        'ETHUSDT': 4,   # ETH: до 0.0001
        'SOLUSDT': 2,   # SOL: до 0.01
        'XRPUSDT': 0,   # XRP: целые числа
        'DOGEUSDT': 0,  # DOGE: целые числа
        'ADAUSDT': 0,   # ADA: целые числа
        'AVAXUSDT': 2,
        'LINKUSDT': 2,
        'DOTUSDT': 2,
        'MATICUSDT': 0,
    }
    
    def __init__(self):
        self.client = HTTP(
            testnet=True,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
            recv_window=20000  # Увеличенный timeout для стабильности
        )
        self.trades_repo = TradesRepository()
        self.pnl_repo = PnLRepository()
    
    def _format_qty(self, pair: str, quantity: float) -> str:
        """
        Форматировать quantity для Bybit API.
        Решает ошибку: Order quantity has too many decimals (170137)
        """
        precision = self.QTY_PRECISION.get(pair, 2)  # По умолчанию 2 знака
    
        # Используем floor, чтобы никогда не округлять вверх (избегаем Insufficient balance)
        factor = 10 ** precision
        rounded = math.floor(quantity * factor) / factor
    
        if precision == 0:
            return str(int(rounded))
        
        # Форматируем и убираем лишние нули
        formatted = f"{rounded:.{precision}f}".rstrip('0').rstrip('.')
        return formatted if formatted else "0"
    
    # Кэш точности для цен (количество десятичных знаков)
    PRICE_PRECISION = {
        'BTCUSDT': 2,   # BTC: до $0.01
        'ETHUSDT': 2,   # ETH: до $0.01
        'SOLUSDT': 2,   # SOL: до $0.01
        'XRPUSDT': 4,   # XRP: до $0.0001
        'DOGEUSDT': 5,  # DOGE: до $0.00001
        'ADAUSDT': 4,   # ADA: до $0.0001
        'AVAXUSDT': 2,
        'LINKUSDT': 2,
        'DOTUSDT': 2,
        'MATICUSDT': 4,
    }
    
    def _format_price(self, pair: str, price: float) -> str:
        """
        Форматировать price для Bybit API.
        Решает ошибку: Order price has too many decimals (170134)
        """
        precision = self.PRICE_PRECISION.get(pair, 2)  # По умолчанию 2 знака
        rounded = round(price, precision)
        
        # Форматируем и убираем лишние нули
        formatted = f"{rounded:.{precision}f}".rstrip('0').rstrip('.')
        return formatted if formatted else "0"
    
    def _get_base_coin(self, pair: str) -> str:
        """Извлечь базовую монету из пары (BTCUSDT -> BTC)"""
        return pair.replace('USDT', '').replace('USDC', '')
    
    def _get_actual_balance(self, pair: str) -> float:
        """
        Получить реальный ДОСТУПНЫЙ баланс монеты на бирже.
        Решает ошибку: Insufficient balance (170131)
        """
        try:
            base_coin = self._get_base_coin(pair)
            wallet = self.client.get_wallet_balance(
                accountType="UNIFIED",
                coin=base_coin
            )
            
            if wallet['retCode'] == 0 and wallet['result']['list']:
                coins = wallet['result']['list'][0].get('coin', [])
                for coin in coins:
                    if coin['coin'] == base_coin:
                        # Используем 'free' (доступный для торговли) вместо 'walletBalance'
                        # free = доступно для торговли, walletBalance = общий баланс
                        free_balance = float(coin.get('free', 0) or coin.get('availableToWithdraw', 0) or 0)
                        wallet_balance = float(coin.get('walletBalance', 0))
                        
                        logger.debug(f"Баланс {base_coin}: free={free_balance}, wallet={wallet_balance}")
                        
                        # Возвращаем меньшее из двух (безопаснее)
                        return min(free_balance, wallet_balance) if free_balance > 0 else wallet_balance
            return 0.0
        except Exception as e:
            logger.debug(f"Ошибка получения баланса {pair}: {e}")
            return -1  # -1 означает ошибку, не 0

    async def sync_orders_and_trades(self, max_orders: int = 50) -> None:
        open_orders = self.trades_repo.get_open_orders()
        if not open_orders:
            return

        def _parse_dt(value: str) -> datetime:
            try:
                return datetime.fromisoformat(value.replace('Z', '+00:00'))
            except Exception:
                return datetime.min

        open_orders_sorted = sorted(open_orders, key=lambda o: _parse_dt(o.get('created_at', '')), reverse=True)
        for order in open_orders_sorted[:max_orders]:
            order_id = order.get('order_id')
            pair = order.get('pair')
            if not order_id or not pair:
                continue

            try:
                order_history = self.client.get_order_history(
                    category="spot",
                    symbol=pair,
                    orderId=order_id
                )

                if order_history.get('retCode') == 0:
                    items = (((order_history.get('result') or {}).get('list')) or [])
                    if items:
                        item = items[0]
                        status = item.get('orderStatus') or item.get('order_status') or item.get('status')
                        filled_qty_raw = item.get('cumExecQty') or item.get('cum_exec_qty') or item.get('filledQty')
                        avg_price_raw = item.get('avgPrice') or item.get('avg_price')

                        filled_qty = None
                        avg_price = None
                        try:
                            if filled_qty_raw is not None:
                                filled_qty = float(filled_qty_raw)
                        except Exception:
                            filled_qty = None

                        try:
                            if avg_price_raw is not None:
                                avg_price = float(avg_price_raw)
                        except Exception:
                            avg_price = None

                        if status:
                            self.trades_repo.update_order_status(order_id, status, filled_qty=filled_qty, avg_price=avg_price)

                executions = self.client.get_executions(
                    category="spot",
                    symbol=pair,
                    orderId=order_id
                )

                if executions.get('retCode') == 0:
                    exec_items = (((executions.get('result') or {}).get('list')) or [])
                    for ex in exec_items:
                        trade_id = ex.get('execId') or ex.get('exec_id') or ex.get('tradeId')
                        if not trade_id:
                            continue

                        exec_price = ex.get('execPrice') or ex.get('price')
                        exec_qty = ex.get('execQty') or ex.get('qty')
                        exec_fee = ex.get('execFee') or ex.get('fee')
                        fee_coin = ex.get('feeRate') or ex.get('feeCoin') or ex.get('fee_coin')
                        exec_time = ex.get('execTime') or ex.get('exec_time') or ex.get('tradeTime')
                        side = ex.get('side') or order.get('side')

                        try:
                            price_val = float(exec_price) if exec_price is not None else 0.0
                            qty_val = float(exec_qty) if exec_qty is not None else 0.0
                            fee_val = float(exec_fee) if exec_fee is not None else 0.0
                        except Exception:
                            continue

                        position_id = self.trades_repo.get_order_position_id(order_id)
                        trade_data = {
                            'trade_id': str(trade_id),
                            'order_id': str(order_id),
                            'position_id': position_id,
                            'pair': pair,
                            'side': side,
                            'price': price_val,
                            'quantity': qty_val,
                            'fee': fee_val,
                            'fee_asset': 'USDT' if not fee_coin else str(fee_coin),
                            'executed_at': str(exec_time) if exec_time else datetime.now(TIMEZONE).isoformat(),
                        }
                        self.trades_repo.create_trade_if_not_exists(trade_data)

            except Exception as e:
                logger.debug(f"sync_orders_and_trades error for {pair} {order_id}: {e}")
    
    def _close_position_with_pnl(self, position_id: int, pair: str, 
                                   exit_price: float, realized_pnl: float) -> None:
        """
        Закрыть позицию в БД и записать PnL в статистику.
        Единая точка закрытия позиций для корректной записи отчётов.
        """
        # Закрываем позицию в БД
        self.trades_repo.close_position(position_id, exit_price, realized_pnl)
        
        # Записываем в статистику PnL
        today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        is_win = realized_pnl > 0
        self.pnl_repo.record_daily_pnl(today, pair, realized_pnl, is_win)
        
        logger.debug(f"📊 PnL записан: {pair} | {realized_pnl:.2f}")

    async def update_positions_prices(self) -> None:
        """Обновить текущие цены всех открытых позиций (через WS)"""
        open_positions = self.trades_repo.get_open_positions()
        
        for position in open_positions:
            try:
                pair = position['pair']
                # Используем WS цену
                current_price = websocket_service.get_price(pair)
                
                if current_price > 0:
                    self.trades_repo.update_position_price(position['id'], current_price)
                    
            except Exception as e:
                logger.debug(f"Ошибка обновления цены {position['pair']}: {e}")

    async def update_trailing_stops(self) -> List[str]:
        """
        Обновить Trailing Stops для прибыльных позиций
        Returns: Список сообщений о передвинутых стопах
        """
        messages = []
        open_positions = self.trades_repo.get_open_positions()
        
        for pos in open_positions:
            pair = pos['pair']
            entry = pos['entry_price']
            curr_sl = pos['sl_price']
            
            # Получаем актуальную цену
            current_price = pos['current_price']
            
            # Если цена не обновлена, пропускаем
            if not current_price or current_price <= 0:
                continue
            
            # Расчёт профита
            profit_pct = (current_price - entry) / entry
            
            # Если профит больше порога активации
            if profit_pct > TRAILING_ACTIVATION:
                # Рассчитываем новый SL (на расстоянии TRAILING_STEP)
                new_sl = current_price * (1 - TRAILING_STEP)
                
                # Двигаем SL только вверх
                if new_sl > curr_sl:
                    try:
                        # 1. Находим активный SL ордер
                        sl_order = self.trades_repo.get_sl_order(pos['id'])
                        if not sl_order:
                            logger.warning(f"⚠️ Нет активного SL ордера для {pair} (ID: {pos['id']})")
                            continue
                            
                        # 2. Изменяем ордер на бирже
                        response = self.client.amend_order(
                            category="spot",
                            symbol=pair,
                            orderId=sl_order['order_id'],
                            triggerPrice=self._format_price(pair, new_sl)
                        )
                        
                        if response['retCode'] == 0:
                            # 3. Обновляем в БД
                            self.trades_repo.set_position_tpsl(pos['id'], sl_price=new_sl)
                            self.trades_repo.update_order_price(sl_order['order_id'], new_sl)
                            
                            msg = f"🔄 SL по {pair} подтянут до {new_sl:.4f} (Прибыль: {profit_pct*100:.2f}%)"
                            logger.info(msg)
                            messages.append(msg)
                        else:
                            logger.error(f"❌ Ошибка изменения SL {pair}: {response['retMsg']}")
                            
                    except Exception as e:
                        logger.error(f"❌ Ошибка Trailing Stop {pair}: {e}")
        
        return messages

    async def close_position(self, position_id: int, reason: str = "manual") -> bool:
        """Закрыть позицию вручную"""
        position = self.trades_repo.get_position_by_id(position_id)
        
        if not position or position['status'] != 'OPEN':
            logger.warning(f"⚠️ Позиция {position_id} не найдена или уже закрыта")
            return False
        
        pair = position['pair']
        quantity = position['quantity']
        entry_price = position['entry_price']
        
        try:
            # Получаем текущую цену
            ticker = self.client.get_tickers(category="spot", symbol=pair)
            current_price = float(ticker['result']['list'][0]['lastPrice'])
            
            # ====== НОВОЕ: Проверка реального баланса ======
            actual_balance = self._get_actual_balance(pair)
            
            if actual_balance >= 0 and actual_balance < quantity * 0.99:  # 1% допуск
                # Баланса недостаточно - закрываем только в БД
                logger.warning(
                    f"⚠️ Недостаточно {self._get_base_coin(pair)}: "
                    f"есть {actual_balance:.6f}, нужно {quantity:.6f}. "
                    f"Закрываем позицию в БД без торговли."
                )
                
                # Если есть хоть какой-то баланс - пробуем продать его
                if actual_balance > 0:
                    sell_qty = self._format_qty(pair, actual_balance)
                    try:
                        self.client.place_order(
                            category="spot",
                            symbol=pair,
                            side="Sell",
                            orderType="Market",
                            qty=sell_qty,
                            marketUnit="baseCoin"
                        )
                        logger.info(f"✅ Продано остаток: {sell_qty} {self._get_base_coin(pair)}")
                    except Exception as e:
                        logger.debug(f"Не удалось продать остаток: {e}")
                
                # Закрываем позицию в БД с приблизительным PnL
                realized_pnl = (current_price - entry_price) * actual_balance
                self._close_position_with_pnl(position_id, pair, current_price, realized_pnl)
                await self._cancel_position_orders(position_id)
                return True
            # ====== КОНЕЦ НОВОЙ ЛОГИКИ ======
            
            # Форматируем qty правильно (без лишних decimals)
            formatted_qty = self._format_qty(pair, quantity)
            
            # Размещаем рыночный ордер на продажу
            order_response = self.client.place_order(
                category="spot",
                symbol=pair,
                side="Sell",
                orderType="Market",
                qty=formatted_qty,
                marketUnit="baseCoin"
            )
            
            if order_response['retCode'] != 0:
                logger.error(f"❌ Ошибка закрытия: {order_response['retMsg']}")
                return False
            
            # Расчёт PnL
            realized_pnl = (current_price - entry_price) * quantity
            
            # Закрываем позицию с записью PnL
            self._close_position_with_pnl(position_id, pair, current_price, realized_pnl)
            
            logger.info(f"✅ Позиция закрыта: {pair} | PnL: {realized_pnl:.2f} | Причина: {reason}")
            
            # Отменяем открытые TP/SL ордера
            await self._cancel_position_orders(position_id)
            
            return True
            
        except Exception as e:
            error_tracker.add_error("Bybit", "CloseError", str(e))
            logger.error(f"❌ Ошибка закрытия позиции: {e}")
            return False
    
    async def _cancel_position_orders(self, position_id: int) -> None:
        """Отменить все открытые ордера позиции"""
        try:
            # Получаем открытые ордера по позиции
            from ..db.connection import get_db
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT order_id, pair FROM orders
                    WHERE position_id = ? AND status IN ('New', 'PartiallyFilled')
                """, (position_id,))
                
                orders = cursor.fetchall()
            
            for order in orders:
                try:
                    self.client.cancel_order(
                        category="spot",
                        symbol=order['pair'],
                        orderId=order['order_id']
                    )
                    
                    self.trades_repo.update_order_status(order['order_id'], 'Cancelled')
                    
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось отменить ордер {order['order_id']}: {e}")
                    
        except Exception as e:
            logger.error(f"❌ Ошибка отмены ордеров: {e}")
    
    async def check_breakeven(self) -> List[str]:
        """
        Проверить и активировать Breakeven для позиций с прибылью >= 1%
        Returns: Список сообщений о перенесённых стопах
        """
        if not BREAKEVEN_ENABLED:
            return []
        
        messages = []
        open_positions = self.trades_repo.get_open_positions()
        
        for pos in open_positions:
            # Пропускаем если breakeven уже активирован
            if pos.get('breakeven_activated', 0) == 1:
                continue
            
            pair = pos['pair']
            entry = pos.get('avg_entry_price') or pos['entry_price']
            current_price = pos['current_price']
            
            if not current_price or current_price <= 0:
                continue
            
            # Расчёт профита
            profit_pct = (current_price - entry) / entry
            
            # Если прибыль >= порога активации breakeven
            if profit_pct >= BREAKEVEN_TRIGGER_PERCENT:
                # Новый SL = цена входа + небольшой буфер
                new_sl = entry * (1 + BREAKEVEN_BUFFER)
                
                try:
                    # Находим активный SL ордер
                    sl_order = self.trades_repo.get_sl_order(pos['id'])
                    if not sl_order:
                        logger.warning(f"⚠️ Нет SL ордера для breakeven {pair}")
                        continue
                    
                    # Изменяем ордер на бирже
                    response = self.client.amend_order(
                        category="spot",
                        symbol=pair,
                        orderId=sl_order['order_id'],
                        triggerPrice=self._format_price(pair, new_sl)
                    )
                    
                    if response['retCode'] == 0:
                        # Обновляем в БД
                        self.trades_repo.set_position_tpsl(pos['id'], sl_price=new_sl)
                        self.trades_repo.update_order_price(sl_order['order_id'], new_sl)
                        self._set_breakeven_flag(pos['id'])
                        
                        msg = f"🛡️ BREAKEVEN {pair}: SL перенесён в {new_sl:.4f} (вход: {entry:.4f})"
                        logger.info(msg)
                        messages.append(msg)
                    else:
                        error_msg = response.get('retMsg', '')
                        ret_code = response.get('retCode', 0)
                        
                        # Ордер не существует на бирже — удаляем из БД
                        if ret_code == 170213 or 'does not exist' in error_msg:
                            logger.warning(f"⚠️ SL ордер {pair} не найден на бирже, удаляем из БД")
                            self.trades_repo.update_order_status(sl_order['order_id'], 'Cancelled')
                            # auto_create_missing_sl пересоздаст ордер
                        else:
                            logger.error(f"❌ Ошибка breakeven {pair}: {error_msg}")
                        
                except Exception as e:
                    error_str = str(e)
                    # Ордер не существует — удаляем из БД
                    if '170213' in error_str or 'does not exist' in error_str:
                        logger.warning(f"⚠️ SL ордер {pair} не найден на бирже, удаляем из БД")
                        sl_order = self.trades_repo.get_sl_order(pos['id'])
                        if sl_order:
                            self.trades_repo.update_order_status(sl_order['order_id'], 'Cancelled')
                    else:
                        logger.error(f"❌ Ошибка breakeven {pair}: {e}")
        
        return messages
    
    def _set_breakeven_flag(self, position_id: int) -> None:
        """Установить флаг breakeven для позиции"""
        from ..db.connection import get_transaction
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE positions SET breakeven_activated = 1 WHERE id = ?
            """, (position_id,))
    
    async def check_time_exit(self) -> List[str]:
        """
        Закрыть позиции старше MAX_POSITION_HOURS без значимого движения
        Returns: Список сообщений о закрытых позициях
        """
        if not TIME_EXIT_ENABLED:
            return []
        
        messages = []
        open_positions = self.trades_repo.get_open_positions()
        now = datetime.now(TIMEZONE)
        
        for pos in open_positions:
            pair = pos['pair']
            entry = pos.get('avg_entry_price') or pos['entry_price']
            current_price = pos['current_price']
            
            if not current_price or current_price <= 0:
                continue
            
            # Парсим время открытия
            try:
                opened_at = datetime.fromisoformat(pos['opened_at'].replace('Z', '+00:00'))
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=TIMEZONE)
            except Exception:
                continue
            
            # Проверяем возраст позиции
            position_age_hours = (now - opened_at).total_seconds() / 3600
            
            if position_age_hours < MAX_POSITION_HOURS:
                continue
            
            # Проверяем движение цены
            move_pct = abs((current_price - entry) / entry)
            
            if move_pct < STALE_MOVE_THRESHOLD:
                # Позиция "мёртвая" - закрываем
                try:
                    closed = await self.close_position(pos['id'], reason="time_exit")
                    
                    if closed:
                        msg = f"⏰ TIME EXIT {pair}: закрыта после {position_age_hours:.1f}ч (движение: {move_pct*100:.2f}%)"
                        logger.info(msg)
                        messages.append(msg)
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка time exit {pair}: {e}")
        
        return messages
    
    async def auto_create_missing_sl(self) -> List[str]:
        """
        Автоматически создать SL ордера для позиций без них.
        Синхронизирует qty в БД с реальным балансом на бирже.
        
        Returns: Список сообщений о созданных ордерах
        """
        messages = []
        open_positions = self.trades_repo.get_open_positions()
        
        for pos in open_positions:
            pair = pos['pair']
            position_id = pos['id']
            
            # Проверяем, есть ли уже SL ордер
            sl_order = self.trades_repo.get_sl_order(position_id)
            if sl_order:
                continue  # SL уже есть
            
            logger.info(f"🔧 AUTO-SL: проверяю {pair} (позиция #{position_id})")
            
            # Нет SL - создаём
            db_quantity = pos['quantity']
            sl_price = pos['sl_price']
            
            if not sl_price or sl_price <= 0:
                # Если SL не задан, рассчитаем от текущей цены (-1%)
                current_price = pos.get('current_price') or pos['entry_price']
                sl_price = current_price * 0.99
            
            try:
                # Проверяем реальный баланс
                actual_balance = self._get_actual_balance(pair)
                logger.debug(f"  Баланс {pair}: DB={db_quantity:.6f}, биржа={actual_balance:.6f}")
                
                if actual_balance < 0:
                    # Ошибка получения баланса
                    logger.warning(f"⚠️ AUTO-SL {pair}: не удалось получить баланс")
                    continue
                
                # Минимальное количество для SL (примерно $10)
                min_qty_value = 10.0
                current_price = pos.get('current_price') or pos['entry_price']
                min_qty = min_qty_value / current_price if current_price > 0 else 0
                
                if actual_balance < min_qty:
                    # Баланса совсем нет - закрываем позицию в БД
                    logger.warning(
                        f"⚠️ AUTO-SL {pair}: баланс меньше минимума "
                        f"({actual_balance:.6f} < {min_qty:.6f}). Закрываем позицию в БД."
                    )
                    self._close_position_with_pnl(position_id, pair, current_price, 0)
                    messages.append(f"🗑️ Позиция {pair} закрыта (нет баланса)")
                    continue
                
                # Если реальный баланс отличается от БД - синхронизируем
                if actual_balance < db_quantity * 0.95:  # Отклонение > 5%
                    logger.info(
                        f"📊 AUTO-SL {pair}: обновляю qty в БД "
                        f"({db_quantity:.6f} → {actual_balance:.6f})"
                    )
                    # Обновляем quantity в позиции
                    self.trades_repo.update_position_quantity(position_id, actual_balance)
                    db_quantity = actual_balance
                
                # Форматируем qty
                formatted_qty = self._format_qty(pair, actual_balance)
                
                # Создаём SL ордер (рыночный с триггером)
                sl_response = self.client.place_order(
                    category="spot",
                    symbol=pair,
                    side="Sell",
                    orderType="Market",
                    qty=formatted_qty,
                    triggerPrice=self._format_price(pair, sl_price),
                    marketUnit="baseCoin"
                )
                
                if sl_response['retCode'] == 0:
                    sl_order_id = sl_response['result']['orderId']
                    
                    # Сохраняем в БД
                    self.trades_repo.create_order({
                        'order_id': sl_order_id,
                        'pair': pair,
                        'side': 'Sell',
                        'order_type': 'Market',
                        'price': sl_price,
                        'quantity': actual_balance,
                        'status': 'New',
                        'is_sl': True,
                        'position_id': position_id,
                        'created_at': datetime.now(TIMEZONE).isoformat(),
                        'updated_at': datetime.now(TIMEZONE).isoformat()
                    })
                    
                    # Обновляем sl_price в позиции
                    self.trades_repo.set_position_tpsl(position_id, sl_price=sl_price)
                    
                    msg = f"✅ AUTO-SL создан: {pair} qty={formatted_qty} @ {sl_price:.2f}"
                    logger.info(msg)
                    messages.append(msg)
                else:
                    error_msg = sl_response.get('retMsg', 'Unknown error')
                    logger.error(f"❌ Ошибка создания SL {pair}: {error_msg}")
                    
                    # Если ошибка Insufficient balance - закрываем позицию в БД
                    if 'Insufficient balance' in error_msg or sl_response['retCode'] == 170131:
                        logger.warning(f"⚠️ Закрываем позицию {pair} в БД (баланс недоступен)")
                        self._close_position_with_pnl(position_id, pair, current_price, 0)
                        messages.append(f"🗑️ Позиция {pair} закрыта (баланс недоступен)")
                    
            except Exception as e:
                error_str = str(e)
                error_tracker.add_error("Bybit", "AutoSLError", error_str)
                logger.error(f"❌ Ошибка auto SL {pair}: {e}")
                
                # Если ошибка Insufficient balance - закрываем позицию в БД
                if 'Insufficient balance' in error_str or '170131' in error_str:
                    logger.warning(f"⚠️ Закрываем позицию {pair} в БД (баланс недоступен)")
                    self._close_position_with_pnl(position_id, pair, current_price, 0)
                    messages.append(f"🗑️ Позиция {pair} закрыта (баланс недоступен)")
        
        return messages


# Глобальный экземпляр
position_service = PositionService()

