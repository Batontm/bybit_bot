"""
Сервис управления открытыми позициями (закрытие, TP/SL, Trailing Stop)
"""
from typing import List, Optional
from datetime import datetime, timedelta
import asyncio
import math
from pybit.unified_trading import HTTP
from config.settings import logger, TIMEZONE
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, BYBIT_ACCOUNT_TYPE, get_pybit_kwargs
from config.trading_config import (
    BREAKEVEN_ENABLED, TRAILING_ENABLED, TIME_EXIT_ENABLED,
    EXIT_PARTIAL_ENABLED, EXIT_PARTIAL_R, EXIT_TRAIL_MIN_STEP, DEFAULT_SL_PERCENT,
)
from . import exit_rules

from ..db.trades_repo import TradesRepository
from ..db.pnl_repo import PnLRepository
from .websocket_service import websocket_service
from .instrument_info import instrument_info
from ..error_tracker import error_tracker



class PositionService:
    """Управление позициями: мониторинг, модификация, закрытие"""
    
    
    def __init__(self):
        self.client = HTTP(
            **get_pybit_kwargs(),
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
            recv_window=20000  # Увеличенный timeout для стабильности
        )
        self.trades_repo = TradesRepository()
        self.pnl_repo = PnLRepository()
    
    def _format_qty(self, pair: str, quantity: float) -> str:
        """Форматировать quantity по шагу инструмента (округление вниз)."""
        return instrument_info.format_qty(pair, quantity)

    def _format_price(self, pair: str, price: float) -> str:
        """Форматировать price по тику инструмента."""
        return instrument_info.format_price(pair, price)
    
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
            account_type = "UNIFIED" if str(BYBIT_ACCOUNT_TYPE).upper() == "UNIFIED" else "SPOT"
            wallet = self.client.get_wallet_balance(
                accountType=account_type,
                coin=base_coin
            )
            
            if wallet['retCode'] == 0 and wallet['result']['list']:
                coins = wallet['result']['list'][0].get('coin', [])
                for coin in coins:
                    if coin['coin'] == base_coin:
                        def _safe_float(v) -> float:
                            if v is None:
                                return 0.0
                            if isinstance(v, (int, float)):
                                return float(v)
                            s = str(v).strip()
                            if s == "":
                                return 0.0
                            try:
                                return float(s)
                            except Exception:
                                return 0.0

                        # Используем 'free' (доступный для торговли) вместо 'walletBalance'
                        # free = доступно для торговли, walletBalance = общий баланс
                        free_balance = _safe_float(coin.get('free', 0) or coin.get('availableToWithdraw', 0) or 0)
                        wallet_balance = _safe_float(coin.get('walletBalance', 0))
                        
                        logger.debug(f"Баланс {base_coin}: free={free_balance}, wallet={wallet_balance}")
                        
                        # Возвращаем меньшее из двух (безопаснее)
                        return min(free_balance, wallet_balance) if free_balance > 0 else wallet_balance
            return 0.0
        except Exception as e:
            logger.debug(f"Ошибка получения баланса {pair}: {e}")
            return -1  # -1 означает ошибку, не 0

    async def _cancel_exchange_open_orders_for_symbol(self, pair: str) -> None:
        try:
            resp = self.client.get_open_orders(category="spot", symbol=pair)
            if resp.get('retCode') != 0:
                return

            orders = (((resp.get('result') or {}).get('list')) or [])
            for o in orders:
                order_id = o.get('orderId') or o.get('order_id')
                if not order_id:
                    continue
                try:
                    self.client.cancel_order(
                        category="spot",
                        symbol=pair,
                        orderId=str(order_id)
                    )
                except Exception:
                    continue
        except Exception:
            return

    async def sync_orders_and_trades(self, max_orders: int = 50) -> List[str]:
        """
        Синхронизировать статусы ордеров и исполнения с биржей.
        Если исполнился TP или SL — закрыть позицию с реальным PnL и отменить
        второй защитный ордер. Returns: сообщения для уведомлений.
        """
        messages: List[str] = []
        open_orders = self.trades_repo.get_open_orders()
        if not open_orders:
            return messages

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

            status = None
            filled_qty = None
            avg_price = None
            exit_fee_usdt = 0.0
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
                    else:
                        # в истории нет — проверяем открытые, включая условные SL,
                        # иначе живой стоп ошибочно помечался Cancelled и плодились дубли
                        if not self._order_alive_on_exchange(pair, order_id):
                            self.trades_repo.update_order_status(order_id, 'Cancelled')

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
                        if str(side) == 'Sell':
                            # комиссия продажи списывается в USDT
                            exit_fee_usdt += fee_val if fee_val < price_val * qty_val else 0.0
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

                if (order.get('is_tp') or order.get('is_sl')) and status in ('Filled', 'PartiallyFilledCanceled') \
                        and filled_qty and avg_price:
                    msg = await self._handle_exit_fill(order, status, filled_qty, avg_price, exit_fee_usdt)
                    if msg:
                        messages.append(msg)

            except Exception as e:
                logger.debug(f"sync_orders_and_trades error for {pair} {order_id}: {e}")

        return messages

    async def _handle_exit_fill(self, order: dict, status: str, filled_qty: float,
                                avg_price: float, exit_fee_usdt: float) -> Optional[str]:
        """Исполнился TP/SL на бирже → закрыть позицию в БД с реальным PnL."""
        position_id = order.get('position_id')
        if not position_id:
            return None
        pos = self.trades_repo.get_position_by_id(position_id)
        if not pos or pos.get('status') != 'OPEN':
            return None

        pair = pos['pair']
        entry = float(pos.get('avg_entry_price') or pos['entry_price'])
        qty = float(pos['quantity'])
        kind = "TP" if order.get('is_tp') else "SL"

        if filled_qty < qty * 0.95:
            remaining = max(0.0, qty - filled_qty)
            part_pnl = (avg_price - entry) * filled_qty - exit_fee_usdt
            self.trades_repo.set_position_fields(
                position_id, quantity=remaining,
                partial_pnl=float(pos.get('partial_pnl') or 0) + part_pnl,
                partial_taken=1 if kind == "TP" else int(pos.get('partial_taken') or 0),
            )
            if kind != "TP":
                logger.info(f"📉 {pair}: SL исполнен частично {filled_qty}/{qty}")
                return None
            # TP1: стоп остатка — в безубыток с запасом на комиссии, объём — на остаток
            new_sl = max(float(pos.get('sl_price') or 0), exit_rules.breakeven_stop(entry))
            moved = await self._amend_sl(position_id, pair, new_sl, remaining)
            sign = "+" if part_pnl >= 0 else "-"
            msg = (f"🎯 TP1 {pair}: зафиксировано {filled_qty:g} @ {avg_price:.6g} "
                   f"({sign}${abs(part_pnl):.2f}); остаток {remaining:g}"
                   + (f", стоп → безубыток {new_sl:.6g}" if moved else ", ⚠️ стоп не перенесён — проверьте"))
            logger.info(msg)
            return msg

        partial = float(pos.get('partial_pnl') or 0)
        gross = (avg_price - entry) * filled_qty + partial
        entry_fee = float(pos.get('commission_paid') or 0)
        fees = entry_fee + exit_fee_usdt
        net = gross - fees
        try:
            self.trades_repo.update_position_pnl_breakdown(
                position_id, commission_paid=fees, slippage=float(pos.get('slippage') or 0),
                gross_pnl=gross, net_pnl=net,
            )
        except Exception as e:
            logger.debug(f"pnl breakdown {pair}: {e}")
        self._close_position_with_pnl(position_id, pair, avg_price, net)
        # второй защитный ордер больше не нужен (иначе он «висит» и блокирует монеты)
        await self._cancel_position_orders(position_id)

        pct = (avg_price - entry) / entry * 100 if entry else 0.0
        if kind == "TP":
            emoji, label = "🎯", "TP"
        elif avg_price >= entry:
            emoji, label = "🛡", "Стоп в плюсе (безубыток/трейлинг)"
        else:
            emoji, label = "🛑", "SL"
        sign = "+" if net >= 0 else "-"
        msg = (f"{emoji} {label} {pair}: позиция закрыта @ {avg_price:.6g} "
               f"({pct:+.2f}%) | PnL за сделку {sign}${abs(net):.2f} (комиссии ${fees:.2f})")
        logger.info(msg)
        return msg

    async def reconcile_orphan_orders(self) -> List[str]:
        """
        Сверка ордеров: биржа vs БД.
        1. Ордер есть на бирже, но нет в БД → отменить на бирже (сирота)
        2. Ордер есть в БД (New/PartiallyFilled), но нет на бирже → пометить Cancelled
        Returns: Список сообщений о действиях
        """
        messages = []

        # --- Шаг 1: получить все открытые ордера на бирже (spot) ---
        exchange_orders = {}  # {order_id: order_dict}
        try:
            # обычные ордера + условные (SL с triggerPrice) — они выдаются отдельно
            for extra in ({}, {"orderFilter": "StopOrder"}, {"orderFilter": "tpslOrder"}):
                resp = self.client.get_open_orders(category="spot", **extra)
                if resp.get('retCode') == 0:
                    for o in (resp.get('result', {}).get('list') or []):
                        oid = o.get('orderId')
                        if oid:
                            exchange_orders[oid] = o
        except Exception as e:
            logger.error(f"❌ reconcile: ошибка получения ордеров с биржи: {e}")
            return [f"❌ Ошибка получения ордеров: {e}"]

        # --- Шаг 2: получить открытые ордера из БД ---
        db_orders = self.trades_repo.get_open_orders()  # status IN ('New','PartiallyFilled')
        db_order_ids = {o['order_id'] for o in db_orders}

        # --- Шаг 3: сироты на бирже (есть на бирже, нет в БД) → отменить ---
        for oid, exo in exchange_orders.items():
            if oid not in db_order_ids:
                pair = exo.get('symbol', '???')
                try:
                    if self._cancel_any_filter(pair, oid):
                        msg = f"🗑️ Ордер-сирота отменён: {pair} {oid}"
                    else:
                        msg = f"⚠️ Не удалось отменить ордер-сироту: {pair} {oid}"
                    logger.warning(msg)
                    messages.append(msg)
                except Exception as e:
                    msg = f"❌ Orphan cancel error: {pair} {oid} — {e}"
                    logger.error(msg)
                    messages.append(msg)

        # --- Шаг 4: пропавшие ордера (есть в БД, нет на бирже) → проверить историю ---
        exchange_oid_set = set(exchange_orders.keys())
        for db_order in db_orders:
            oid = db_order['order_id']
            if oid in exchange_oid_set:
                continue  # ордер жив на бирже, всё ок

            pair = db_order.get('pair', '???')
            try:
                hist = self.client.get_order_history(category="spot", symbol=pair, orderId=oid)
                if hist.get('retCode') == 0:
                    items = (hist.get('result', {}).get('list') or [])
                    if items:
                        status = items[0].get('orderStatus', 'Cancelled')
                        filled_qty_raw = items[0].get('cumExecQty')
                        avg_price_raw = items[0].get('avgPrice')
                        filled_qty = float(filled_qty_raw) if filled_qty_raw else None
                        avg_price = float(avg_price_raw) if avg_price_raw else None
                        self.trades_repo.update_order_status(oid, status, filled_qty=filled_qty, avg_price=avg_price)
                        msg = f"📋 Order {pair} {oid}: DB→{status} (filled={filled_qty})"
                        logger.info(msg)
                        messages.append(msg)
                    else:
                        self.trades_repo.update_order_status(oid, 'Cancelled')
                        msg = f"📋 Order {pair} {oid}: не найден → Cancelled"
                        logger.info(msg)
                        messages.append(msg)
            except Exception as e:
                logger.debug(f"reconcile history check error {pair} {oid}: {e}")

        if not messages:
            messages.append("✅ Reconcile: расхождений не найдено")

        return messages

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
        
        logger.debug(f" PnL записан: {pair} | {realized_pnl:.2f}")

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

    @staticmethod
    def _risk_r(pos: dict) -> float:
        """R позиции: вход − начальный стоп (для старых позиций — текущий стоп или стоп по умолчанию)."""
        entry = float(pos.get('avg_entry_price') or pos['entry_price'])
        initial = pos.get('initial_sl') or pos.get('sl_price')
        if initial and float(initial) < entry:
            return entry - float(initial)
        return entry * DEFAULT_SL_PERCENT

    async def _amend_sl(self, position_id: int, pair: str, new_sl: float,
                        new_qty: Optional[float] = None) -> bool:
        """Изменить цену (и при необходимости объём) активного SL на бирже и в БД."""
        sl_order = self.trades_repo.get_sl_order(position_id)
        if not sl_order:
            logger.warning(f"⚠️ {pair}: нет активного SL для переноса — подхватит Auto-SL")
            self.trades_repo.set_position_tpsl(position_id, sl_price=new_sl)
            return False
        params = dict(category="spot", symbol=pair, orderId=sl_order['order_id'],
                      triggerPrice=self._format_price(pair, new_sl))
        if new_qty is not None:
            params['qty'] = self._format_qty(pair, new_qty)
        for extra in ({"orderFilter": "StopOrder"}, {}, {"orderFilter": "tpslOrder"}):
            try:
                resp = self.client.amend_order(**params, **extra)
            except Exception as e:
                resp = {'retCode': -1, 'retMsg': str(e)}
            if resp.get('retCode') == 0:
                self.trades_repo.set_position_tpsl(position_id, sl_price=new_sl)
                self.trades_repo.update_order_price(sl_order['order_id'], new_sl)
                if new_qty is not None:
                    from ..db.connection import get_transaction
                    with get_transaction() as conn:
                        conn.execute("UPDATE orders SET quantity = ? WHERE order_id = ?",
                                     (new_qty, sl_order['order_id']))
                return True
            msg = str(resp.get('retMsg', ''))
            if resp.get('retCode') == 170213 or 'does not exist' in msg.lower():
                self.trades_repo.update_order_status(sl_order['order_id'], 'Cancelled')
                return False
        logger.error(f"❌ {pair}: не удалось перенести SL: {resp.get('retMsg')}")
        return False

    async def update_trailing_stops(self) -> List[str]:
        """
        Трейлинг остатка позиции после частичной фиксации (TP1): стоп = цена − k×ATR(1h),
        только вверх и только если поднимается хотя бы на EXIT_TRAIL_MIN_STEP.
        Если частичная фиксация выключена — трейлинг включается с +EXIT_PARTIAL_R.
        Уведомлений не шлёт (итог придёт при закрытии позиции).
        """
        if not TRAILING_ENABLED:
            return []
        for pos in self.trades_repo.get_open_positions():
            if pos.get('strategy') == 'TREND':
                continue  # у тренда свой трейлинг по дневным свечам (trend_service)
            pair = pos['pair']
            price = float(pos.get('current_price') or 0)
            if price <= 0:
                continue
            entry = float(pos.get('avg_entry_price') or pos['entry_price'])
            r = self._risk_r(pos)
            active = bool(pos.get('partial_taken')) or (
                not EXIT_PARTIAL_ENABLED and price >= entry + EXIT_PARTIAL_R * r)
            if not active:
                continue
            atr = float(pos.get('entry_atr') or 0) or r
            cur_sl = float(pos.get('sl_price') or 0)
            new_sl = exit_rules.trail_stop(cur_sl, price, atr)
            if new_sl < cur_sl * (1 + EXIT_TRAIL_MIN_STEP) or new_sl >= price:
                continue
            if await self._amend_sl(pos['id'], pair, new_sl):
                logger.info(f"📈 Трейлинг {pair}: SL {cur_sl:.6g} → {new_sl:.6g} (цена {price:.6g})")
        return []

    async def close_position(self, position_id: int, reason: str = "manual") -> bool:
        """Закрыть позицию вручную"""
        position = self.trades_repo.get_position_by_id(position_id)
        
        if not position or position['status'] != 'OPEN':
            logger.warning(f" Позиция {position_id} не найдена или уже закрыта")
            return False
        
        pair = position['pair']
        quantity = position['quantity']
        entry_price = position.get('avg_entry_price') or position['entry_price']
        
        try:
            await self._cancel_position_orders(position_id)
            await self._cancel_exchange_open_orders_for_symbol(pair)
            await asyncio.sleep(0.5)

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
                        err_str = str(e)
                        # Min order size / dust — просто игнорируем, закроем в БД
                        if '170217' in err_str or 'MIN_NOTIONAL' in err_str or 'too small' in err_str.lower():
                            logger.info(f"🧹 Dust {pair}: остаток слишком мал для продажи, закрываем в БД")
                        else:
                            logger.debug(f"Не удалось продать остаток: {e}")
                
                # Закрываем позицию в БД с приблизительным PnL
                realized_pnl = self._record_manual_pnl(position, current_price, actual_balance)
                self._close_position_with_pnl(position_id, pair, current_price, realized_pnl)
                await self._cancel_position_orders(position_id)
                return True
            # ====== КОНЕЦ НОВОЙ ЛОГИКИ ======
            
            formatted_qty = self._format_qty(pair, quantity)

            order_response = self.client.place_order(
                category="spot",
                symbol=pair,
                side="Sell",
                orderType="Market",
                qty=formatted_qty,
                marketUnit="baseCoin"
            )
            
            if order_response['retCode'] != 0:
                if order_response.get('retCode') == 170131 or 'Insufficient balance' in str(order_response.get('retMsg', '')):
                    actual_balance = self._get_actual_balance(pair)
                    if actual_balance > 0:
                        formatted_qty = self._format_qty(pair, actual_balance)
                        order_response = self.client.place_order(
                            category="spot",
                            symbol=pair,
                            side="Sell",
                            orderType="Market",
                            qty=formatted_qty,
                            marketUnit="baseCoin"
                        )
                        if order_response.get('retCode') != 0:
                            logger.error(f"❌ Ошибка закрытия: {order_response['retMsg']}")
                            return False
                    else:
                        logger.error(f"❌ Ошибка закрытия: {order_response['retMsg']}")
                        return False
                else:
                    logger.error(f"❌ Ошибка закрытия: {order_response['retMsg']}")
                    return False
            
            # Расчёт PnL (вход по средней, комиссии: фактическая на входе + оценка 0.1% на выходе)
            realized_pnl = self._record_manual_pnl(position, current_price, quantity)
            
            # Закрываем позицию с записью PnL
            self._close_position_with_pnl(position_id, pair, current_price, realized_pnl)
            
            logger.info(f"✅ Позиция закрыта: {pair} | PnL: {realized_pnl:.2f} | Причина: {reason}")
            
            await self._cancel_position_orders(position_id)
            
            return True
            
        except Exception as e:
            error_tracker.add_error("Bybit", "CloseError", str(e))
            logger.error(f"❌ Ошибка закрытия позиции: {e}")
            return False
    
    def _cancel_any_filter(self, pair: str, order_id: str) -> bool:
        """
        Отменить спот-ордер любого вида. Условные ордера (SL с triggerPrice) на споте
        отменяются только с orderFilter=StopOrder/tpslOrder, обычные — без него.
        """
        for extra in ({}, {"orderFilter": "StopOrder"}, {"orderFilter": "tpslOrder"}):
            try:
                resp = self.client.cancel_order(category="spot", symbol=pair, orderId=order_id, **extra)
                if resp.get('retCode') == 0:
                    return True
            except Exception as e:
                text = str(e)
                # «ордер не существует / уже исполнен» — считаем отменённым
                if '170213' in text or '110001' in text or 'does not exist' in text.lower():
                    return True
        return False

    def _order_alive_on_exchange(self, pair: str, order_id: str) -> bool:
        """Есть ли ордер среди открытых, включая условные (StopOrder / tpslOrder)."""
        for extra in ({}, {"orderFilter": "StopOrder"}, {"orderFilter": "tpslOrder"}):
            try:
                resp = self.client.get_open_orders(category="spot", symbol=pair, orderId=order_id, **extra)
                if resp.get('retCode') == 0 and (resp.get('result') or {}).get('list'):
                    return True
            except Exception:
                continue
        return False

    def _record_manual_pnl(self, position: dict, exit_price: float, qty: float) -> float:
        """PnL ручного/принудительного закрытия по рынку с учётом комиссий; пишет детализацию."""
        entry = float(position.get('avg_entry_price') or position['entry_price'])
        # + уже зафиксированная часть (TP1)
        gross = (exit_price - entry) * qty + float(position.get('partial_pnl') or 0)
        fees = float(position.get('commission_paid') or 0) + exit_price * qty * 0.001
        net = gross - fees
        try:
            self.trades_repo.update_position_pnl_breakdown(
                position['id'], commission_paid=fees, slippage=float(position.get('slippage') or 0),
                gross_pnl=gross, net_pnl=net,
            )
        except Exception as e:
            logger.debug(f"pnl breakdown {position.get('pair')}: {e}")
        return net

    async def _cancel_position_orders(self, position_id: int) -> None:
        """Отменить все открытые ордера позиции"""
        try:
            # Получаем открытые ордера по позиции
            from ..db.connection import get_db
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT order_id, pair FROM orders
                    WHERE position_id = ? AND status IN ('New', 'PartiallyFilled', 'Untriggered', 'Triggered', 'Active')
                """, (position_id,))
                
                orders = cursor.fetchall()
            
            for order in orders:
                if self._cancel_any_filter(order['pair'], order['order_id']):
                    self.trades_repo.update_order_status(order['order_id'], 'Cancelled')
                else:
                    logger.warning(f"⚠️ Не удалось отменить ордер {order['order_id']} ({order['pair']})")
                    
        except Exception as e:
            logger.error(f"❌ Ошибка отмены ордеров: {e}")

    async def emergency_sl_watchdog(self) -> List[str]:
        messages = []
        open_positions = self.trades_repo.get_open_positions()
        for pos in open_positions:
            try:
                position_id = pos['id']
                pair = pos['pair']
                sl_price = pos.get('sl_price')
                current_price = pos.get('current_price')

                if not sl_price or sl_price <= 0:
                    continue
                if not current_price or current_price <= 0:
                    continue

                if current_price <= sl_price:
                    closed = await self.close_position(position_id, reason="emergency_sl")
                    if closed:
                        msg = f"🛑 EMERGENCY SL {pair}: цена {current_price:.6g} ≤ SL {sl_price:.6g} — закрыто маркетом"
                        logger.warning(msg)
                        messages.append(msg)

            except Exception as e:
                logger.error(f"❌ Ошибка emergency SL watchdog: {e}")

        return messages
    
    async def check_breakeven(self) -> List[str]:
        """
        Безубыток при +EXIT_BREAKEVEN_R: стоп → вход + запас на комиссии полного круга.
        Позиции после TP1 уже в безубытке (стоп переносится при исполнении TP1).
        """
        if not BREAKEVEN_ENABLED:
            return []
        messages = []
        for pos in self.trades_repo.get_open_positions():
            if pos.get('breakeven_activated') or pos.get('partial_taken') or pos.get('strategy') == 'TREND':
                continue
            price = float(pos.get('current_price') or 0)
            if price <= 0:
                continue
            entry = float(pos.get('avg_entry_price') or pos['entry_price'])
            lv = exit_rules.levels(entry, entry - self._risk_r(pos))
            if price < lv.breakeven_trigger:
                continue
            new_sl = exit_rules.breakeven_stop(entry)
            if float(pos.get('sl_price') or 0) >= new_sl:
                self._set_breakeven_flag(pos['id'])
                continue
            if await self._amend_sl(pos['id'], pos['pair'], new_sl):
                self._set_breakeven_flag(pos['id'])
                msg = (f"🛡️ Безубыток {pos['pair']}: цена прошла +1R, SL → {new_sl:.6g} "
                       f"(вход {entry:.6g} + запас на комиссии)")
                logger.info(msg)
                messages.append(msg)
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
        """Закрыть позиции старше EXIT_TIME_HOURS, не дошедшие до +EXIT_TIME_MIN_R."""
        if not TIME_EXIT_ENABLED:
            return []
        messages = []
        now = datetime.now(TIMEZONE)
        for pos in self.trades_repo.get_open_positions():
            if pos.get('partial_taken') or pos.get('strategy') == 'TREND':
                continue  # после TP1 остаток защищён безубытком и трейлингом; тренд держится долго
            price = float(pos.get('current_price') or 0)
            if price <= 0:
                continue
            try:
                opened_at = datetime.fromisoformat(pos['opened_at'].replace('Z', '+00:00'))
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=TIMEZONE)
            except Exception:
                continue
            age_h = (now - opened_at).total_seconds() / 3600
            entry = float(pos.get('avg_entry_price') or pos['entry_price'])
            r = self._risk_r(pos)
            if not exit_rules.time_exit_due(age_h, price, entry, r):
                continue
            try:
                if await self.close_position(pos['id'], reason="time_exit"):
                    after = self.trades_repo.get_position_by_id(pos['id']) or {}
                    pnl = float(after.get('net_pnl') or after.get('realized_pnl') or 0)
                    sign = "+" if pnl >= 0 else "-"
                    msg = (f"⏰ Выход по времени {pos['pair']}: {age_h:.0f} ч без движения к цели "
                           f"(сейчас {(price - entry) / r:+.2f}R) | PnL {sign}${abs(pnl):.2f}")
                    logger.info(msg)
                    messages.append(msg)
            except Exception as e:
                logger.error(f"❌ Ошибка time exit {pos['pair']}: {e}")
        return messages
    
    async def place_stop_order(self, pair: str, qty: float, sl_price: float, position_id: int) -> tuple:
        """
        Поставить стоп-лосс как условный спот-ордер orderFilter=StopOrder:
        по документации Bybit он НЕ блокирует монеты до срабатывания, поэтому
        не мешает другим ордерам и не даёт «Insufficient balance».
        Returns: (ok, order_id | текст ошибки)
        """
        try:
            resp = self.client.place_order(
                category="spot", symbol=pair, side="Sell", orderType="Market",
                qty=self._format_qty(pair, qty), triggerPrice=self._format_price(pair, sl_price),
                orderFilter="StopOrder", marketUnit="baseCoin",
            )
        except Exception as e:
            return False, str(e)
        if resp.get('retCode') != 0:
            return False, f"{resp.get('retMsg')} (ErrCode: {resp.get('retCode')})"
        order_id = resp['result']['orderId']
        now = datetime.now(TIMEZONE).isoformat()
        self.trades_repo.create_order({
            'order_id': order_id, 'pair': pair, 'side': 'Sell', 'order_type': 'Market',
            'price': sl_price, 'quantity': qty, 'status': 'Untriggered', 'is_sl': True,
            'position_id': position_id, 'created_at': now, 'updated_at': now,
        })
        self.trades_repo.set_position_tpsl(position_id, sl_price=sl_price)
        return True, order_id

    async def _cancel_tp_orders(self, position_id: int) -> int:
        """Снять лимитные тейки позиции (старые версии ставили их на бирже — они блокировали монеты)."""
        from ..db.connection import get_db
        with get_db() as conn:
            rows = conn.execute("""
                SELECT order_id, pair FROM orders WHERE position_id = ? AND is_tp = 1
                  AND status IN ('New', 'PartiallyFilled', 'Untriggered', 'Triggered', 'Active')
            """, (position_id,)).fetchall()
        n = 0
        for r in rows:
            if self._cancel_any_filter(r['pair'], r['order_id']):
                self.trades_repo.update_order_status(r['order_id'], 'Cancelled')
                n += 1
        return n

    async def auto_create_missing_sl(self) -> List[str]:
        """
        Поставить SL позициям, у которых его нет. Количество — не больше, чем в позиции
        (монеты других стратегий не трогаем) и не больше реального баланса.
        «Insufficient balance» больше НЕ закрывает позицию в БД: монеты на месте,
        их держат ордера — снимаем лимитные тейки и пробуем снова, иначе предупреждаем.
        """
        messages = []
        for pos in self.trades_repo.get_open_positions():
            pair = pos['pair']
            position_id = pos['id']
            if self.trades_repo.get_sl_order(position_id):
                continue

            db_quantity = float(pos['quantity'])
            sl_price = float(pos.get('sl_price') or 0)
            current_price = float(pos.get('current_price') or pos['entry_price'])
            if sl_price <= 0:
                sl_price = current_price * 0.99

            try:
                actual_balance = self._get_actual_balance(pair)   # walletBalance монеты
                if actual_balance < 0:
                    logger.warning(f"⚠️ AUTO-SL {pair}: не удалось получить баланс")
                    continue

                min_qty = 10.0 / current_price if current_price > 0 else 0
                if actual_balance < min_qty:
                    logger.warning(f"⚠️ AUTO-SL {pair}: монет на балансе нет ({actual_balance:.6f}) — "
                                   f"позиция закрыта вне бота, закрываю в БД")
                    self._close_position_with_pnl(position_id, pair, current_price, 0)
                    messages.append(f"🗑️ Позиция {pair} закрыта в БД: монет на бирже нет")
                    continue

                qty = min(db_quantity, actual_balance)
                if qty < db_quantity * 0.95:
                    logger.info(f"📊 AUTO-SL {pair}: количество в БД {db_quantity:.6f} → {qty:.6f} (баланс)")
                    self.trades_repo.update_position_quantity(position_id, qty)

                ok, info = await self.place_stop_order(pair, qty, sl_price, position_id)
                if not ok and ('170131' in info or 'Insufficient' in info):
                    freed = await self._cancel_tp_orders(position_id)
                    if freed:
                        ok, info = await self.place_stop_order(pair, qty, sl_price, position_id)

                if ok:
                    msg = f"✅ AUTO-SL создан: {pair} qty={self._format_qty(pair, qty)} @ {sl_price:.6g}"
                    logger.info(msg)
                    messages.append(msg)
                else:
                    error_tracker.add_error("Bybit", "AutoSLError", f"{pair}: {info}")
                    messages.append(f"⚠️ {pair}: не удалось поставить стоп ({info[:120]}). "
                                    f"Позиция под защитой аварийного стопа бота; проверьте ордера на Bybit.")
            except Exception as e:
                error_tracker.add_error("Bybit", "AutoSLError", f"{pair}: {e}")
        return messages

    async def rebuild_protection(self) -> List[str]:
        """
        Однократно при старте v2.6: у открытых позиций снять лимитные тейки
        (они блокировали монеты, из-за чего стоп не ставился или не мог исполниться)
        и переставить стоп как StopOrder. Тейки дальше ведёт бот (check_take_profits).
        """
        messages = []
        for pos in self.trades_repo.get_open_positions():
            pair, pid = pos['pair'], pos['id']
            removed = await self._cancel_tp_orders(pid)
            sl = self.trades_repo.get_sl_order(pid)
            if sl:
                if self._cancel_any_filter(pair, sl['order_id']):
                    self.trades_repo.update_order_status(sl['order_id'], 'Cancelled')
            qty = min(float(pos['quantity']), max(0.0, self._get_actual_balance(pair)))
            sl_price = float(pos.get('sl_price') or 0) or float(pos['entry_price']) * 0.99
            if qty > 0:
                ok, info = await self.place_stop_order(pair, qty, sl_price, pid)
                messages.append(f"🔧 {pair}: тейков снято {removed}, стоп переставлен "
                                f"{'✅' if ok else '⚠️ ' + info[:80]}")
        return messages

    async def check_take_profits(self) -> List[str]:
        """
        Тейки ведёт бот: при цене ≥ TP1 продаёт часть позиции по рынку, при цене ≥ TP2 —
        остаток. На Bybit спот без VIP комиссия maker = taker = 0.1%, так что по комиссии
        это то же самое, что лимитка, но монеты не заблокированы и стоп всегда может исполниться.
        """
        from config.trading_config import EXIT_PARTIAL_FRACTION
        messages = []
        for pos in self.trades_repo.get_open_positions():
            if (pos.get('strategy') or 'FUNNEL') == 'TREND':
                continue  # у трендовой стратегии нет тейка — выход только по трейлингу
            price = float(pos.get('current_price') or 0)
            if price <= 0:
                continue
            pair, pid = pos['pair'], pos['id']
            qty = float(pos['quantity'])
            tp1 = float(pos.get('tp1_price') or 0)
            tp2 = float(pos.get('tp_price') or 0)
            if tp2 > 0 and price >= tp2:
                sell_qty, label = qty, "TP"
            elif EXIT_PARTIAL_ENABLED and tp1 > 0 and not pos.get('partial_taken') and price >= tp1:
                sell_qty, label = qty * EXIT_PARTIAL_FRACTION, "TP1"
                if sell_qty * price < 5.0 or (qty - sell_qty) * price < 5.0:
                    continue   # части меньше минимального ордера — ждём полный тейк
            else:
                continue
            filled, avg, fee = await self._market_sell(pair, sell_qty)
            if filled <= 0:
                messages.append(f"⚠️ {pair}: не удалось продать по тейку {label} — повторю")
                continue
            msg = await self._handle_exit_fill({'position_id': pid, 'is_tp': True}, 'Filled', filled, avg, fee)
            if msg:
                messages.append(msg)
        return messages

    async def _market_sell(self, pair: str, qty: float) -> tuple:
        """Рыночная продажа; (исполнено, средняя цена, комиссия в USDT)."""
        try:
            resp = self.client.place_order(category="spot", symbol=pair, side="Sell", orderType="Market",
                                           qty=self._format_qty(pair, qty), marketUnit="baseCoin")
        except Exception as e:
            error_tracker.add_error("Bybit", "TPSellError", f"{pair}: {e}")
            return 0.0, 0.0, 0.0
        if resp.get('retCode') != 0:
            error_tracker.add_error("Bybit", "TPSellError", f"{pair}: {resp.get('retMsg')}")
            return 0.0, 0.0, 0.0
        order_id = resp['result']['orderId']
        for _ in range(10):
            await asyncio.sleep(1)
            try:
                r = self.client.get_order_history(category="spot", symbol=pair, orderId=order_id)
                items = (r.get('result') or {}).get('list') or []
                if items and items[0].get('orderStatus') in ('Filled', 'PartiallyFilledCanceled', 'Cancelled', 'Rejected'):
                    it = items[0]
                    return float(it.get('cumExecQty') or 0), float(it.get('avgPrice') or 0), float(it.get('cumExecFee') or 0)
            except Exception:
                continue
        return 0.0, 0.0, 0.0


# Глобальный экземпляр
position_service = PositionService()

