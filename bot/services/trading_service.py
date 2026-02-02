"""
Сервис торговли - открытие/закрытие позиций, TP/SL
"""
from typing import Dict, Optional, Tuple, List
from datetime import datetime, timedelta
import math
from pybit.unified_trading import HTTP
from config.settings import logger, TIMEZONE
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET
from config.trading_config import (
    MAX_RISK_PER_TRADE,
    MAX_TOTAL_RISK,
    MAX_DAILY_LOSS,
    MAX_OPEN_POSITIONS,
    MAX_ACTIVE_PAIRS,
    MAX_NEW_TRADES_PER_DAY,
    DEFAULT_TP_PERCENT,
    DEFAULT_SL_PERCENT,
    MIN_ORDER_SIZE_USD,
    ATR_BASED_TPSL_ENABLED,
    ATR_PERIOD,
    ATR_TIMEFRAME,
    ATR_TP_MULTIPLIER,
    ATR_SL_MULTIPLIER,
    TREND_FILTER_ENABLED,
    TREND_EMA_FAST,
    TREND_EMA_SLOW,
    PYRAMIDING_ENABLED,
    INITIAL_POSITION_PERCENT,
    PYRAMIDING_TRIGGER,
    PYRAMIDING_ADD_PERCENT,
    DCA_ENABLED,
    DCA_TRIGGER_PERCENT,
    DCA_MAX_ENTRIES,
    DCA_MIN_SCORE,
    DCA_POSITION_MULTIPLIER
)
from ..db.trades_repo import TradesRepository
from ..db.pnl_repo import PnLRepository
from .balance_service import balance_service
from ..error_tracker import error_tracker


class TradingService:
    """Управление торговыми операциями"""
    
    def __init__(self):
        self.client = HTTP(
            testnet=BYBIT_TESTNET,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
            recv_window=20000  # Увеличенный timeout
        )
        self.trades_repo = TradesRepository()
        self.pnl_repo = PnLRepository()
    
    def _format_qty(self, pair: str, quantity: float) -> str:
        """
        Форматировать quantity для Bybit API.
        Решает ошибку: Order quantity has too many decimals (170137)
        """
        precision = self._get_qty_precision(pair)
    
        # Используем floor, чтобы никогда не округлять вверх (избегаем Insufficient balance)
        factor = 10 ** precision
        rounded = math.floor(quantity * factor) / factor
        
        if precision == 0:
            return str(int(rounded))
        
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
        
        formatted = f"{rounded:.{precision}f}".rstrip('0').rstrip('.')
        return formatted if formatted else "0"
    
    def check_risk_limits(self) -> Tuple[bool, str]:
        """
        Проверить все риск-лимиты перед открытием позиции
        Returns: (можно_открывать, причина_если_нет)
        """
        # 1. Проверка количества открытых позиций
        open_positions = self.trades_repo.get_open_positions()
        if len(open_positions) >= MAX_OPEN_POSITIONS:
            return False, f"Достигнут лимит открытых позиций ({MAX_OPEN_POSITIONS})"
        
        # 2. Проверка количества активных пар
        active_pairs = set(pos['pair'] for pos in open_positions)
        if len(active_pairs) >= MAX_ACTIVE_PAIRS:
            return False, f"Достигнут лимит активных пар ({MAX_ACTIVE_PAIRS})"
        
        # 3. Проверка дневного лимита новых сделок
        today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        daily_pnl_data = self.pnl_repo.get_pnl_by_days(days=1)
        
        if daily_pnl_data:
            trades_today = daily_pnl_data[0].get('daily_trades', 0)
            if trades_today >= MAX_NEW_TRADES_PER_DAY:
                return False, f"Достигнут дневной лимит сделок ({MAX_NEW_TRADES_PER_DAY})"
        
        # 4. Проверка дневной просадки
        daily_pnl = self.pnl_repo.get_current_day_pnl()
        deposit = balance_service.get_deposit_usdt()
        
        if deposit > 0:
            daily_loss_percent = abs(daily_pnl / deposit)
            if daily_pnl < 0 and daily_loss_percent >= MAX_DAILY_LOSS:
                return False, f"Достигнут дневной лимит убытка ({MAX_DAILY_LOSS*100}%)"
        
        # 5. Проверка суммарного риска по открытым позициям
        total_risk = sum(
            abs(pos.get('unrealized_pnl', 0)) 
            for pos in open_positions
        )
        
        if deposit > 0:
            total_risk_percent = total_risk / deposit
            if total_risk_percent >= MAX_TOTAL_RISK:
                return False, f"Достигнут лимит суммарного риска ({MAX_TOTAL_RISK*100}%)"
        
        return True, "Все лимиты в норме"
    
    def can_open_position_for_pair(self, pair: str) -> Tuple[bool, str]:
        """
        Проверить, можно ли открыть позицию по паре
        Returns: (можно, причина_если_нет)
        """
        open_positions = self.trades_repo.get_open_positions(pair=pair)
        
        if open_positions:
            return False, f"Уже есть открытая позиция по {pair}"
        
        return True, "OK"
    
    async def open_position(self, pair: str, analysis: Dict) -> Tuple[Optional[Dict], Optional[str]]:
        """
        Открыть позицию по результатам анализа
        
        Args:
            pair: Торговая пара
            analysis: Результат анализа с score и signal
        
        Returns:
            (Dict с данными позиции или None, текст ошибки если None)
        """
        # Проверка риск-лимитов
        can_trade, reason = self.check_risk_limits()
        if not can_trade:
            logger.warning(f"⚠️ Не могу открыть позицию: {reason}")
            return None, reason
        
        # Проверка, что по паре нет открытых позиций
        can_open, reason = self.can_open_position_for_pair(pair)
        if not can_open:
            logger.warning(f"⚠️ {reason}")
            return None, reason
        
        # TREND FILTER: не торговать против тренда
        if TREND_FILTER_ENABLED:
            try:
                klines = self.client.get_kline(
                    category="spot",
                    symbol=pair,
                    interval="60",  # 1h для EMA
                    limit=TREND_EMA_SLOW + 5
                )
                
                if klines['retCode'] == 0 and klines['result']['list']:
                    candles = list(reversed(klines['result']['list']))
                    closes = [float(c[4]) for c in candles]
                    
                    from .indicators_service import TechnicalIndicators
                    ema_fast = TechnicalIndicators.calculate_ema(closes, TREND_EMA_FAST)
                    ema_slow = TechnicalIndicators.calculate_ema(closes, TREND_EMA_SLOW)
                    
                    if ema_fast and ema_slow:
                        if ema_fast < ema_slow:
                            reason = f"TREND FILTER: {pair} в даунтренде (EMA{TREND_EMA_FAST}={ema_fast:.2f} < EMA{TREND_EMA_SLOW}={ema_slow:.2f})"
                            logger.info(f"🚫 {reason}")
                            return None, reason
                        else:
                            logger.debug(f"✅ TREND OK: {pair} в аптренде")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка тренд-фильтра: {e}")
        
        # Получаем текущую цену
        try:
            ticker = self.client.get_tickers(category="spot", symbol=pair)
            if ticker['retCode'] != 0:
                reason = f"Не удалось получить цену {pair}: {ticker['retMsg']}"
                logger.error(f"❌ {reason}")
                return None, reason
            
            current_price = float(ticker['result']['list'][0]['lastPrice'])
            
        except Exception as e:
            error_tracker.add_error("Bybit", "PriceError", str(e))
            return None, str(e)
        
        # Расчёт TP и SL (ATR-based или фиксированный)
        if ATR_BASED_TPSL_ENABLED:
            try:
                # Получаем свечные данные для расчёта ATR
                klines = self.client.get_kline(
                    category="spot",
                    symbol=pair,
                    interval=ATR_TIMEFRAME,
                    limit=ATR_PERIOD + 2
                )
                
                if klines['retCode'] == 0 and klines['result']['list']:
                    # Bybit возвращает данные от новых к старым, переворачиваем
                    candles = list(reversed(klines['result']['list']))
                    
                    highs = [float(c[2]) for c in candles]   # High
                    lows = [float(c[3]) for c in candles]    # Low
                    closes = [float(c[4]) for c in candles]  # Close
                    
                    # Импортируем и вычисляем ATR
                    from .indicators_service import TechnicalIndicators
                    atr = TechnicalIndicators.calculate_atr(highs, lows, closes, ATR_PERIOD)
                    
                    if atr and atr > 0:
                        tp_price = current_price + (atr * ATR_TP_MULTIPLIER)
                        sl_price = current_price - (atr * ATR_SL_MULTIPLIER)
                        
                        tp_percent = ((tp_price - current_price) / current_price) * 100
                        sl_percent = ((current_price - sl_price) / current_price) * 100
                        
                        logger.info(f"📊 ATR-based TP/SL: ATR={atr:.2f}, TP=+{tp_percent:.2f}%, SL=-{sl_percent:.2f}%")
                    else:
                        # Fallback к фиксированным
                        tp_price = current_price * (1 + DEFAULT_TP_PERCENT)
                        sl_price = current_price * (1 - DEFAULT_SL_PERCENT)
                        logger.warning(f"⚠️ ATR=0, используем фиксированные TP/SL")
                else:
                    tp_price = current_price * (1 + DEFAULT_TP_PERCENT)
                    sl_price = current_price * (1 - DEFAULT_SL_PERCENT)
                    logger.warning(f"⚠️ Не удалось получить klines, используем фиксированные TP/SL")
                    
            except Exception as e:
                tp_price = current_price * (1 + DEFAULT_TP_PERCENT)
                sl_price = current_price * (1 - DEFAULT_SL_PERCENT)
                logger.warning(f"⚠️ Ошибка ATR ({e}), используем фиксированные TP/SL")
        else:
            tp_price = current_price * (1 + DEFAULT_TP_PERCENT)
            sl_price = current_price * (1 - DEFAULT_SL_PERCENT)
        
        # Расчёт размера позиции
        position_size = balance_service.calculate_position_size(
            MAX_RISK_PER_TRADE,
            current_price,
            sl_price
        )
        
        if position_size == 0:
            reason = "Не удалось рассчитать размер позиции (возможно ноль или ошибка баланса)"
            logger.error(f"❌ {reason}")
            return None, reason
        
        # ПИРАМИДИНГ: открываем только 65% от расчётного размера
        full_position_size = position_size  # Сохраняем полный размер
        if PYRAMIDING_ENABLED:
            position_size = position_size * INITIAL_POSITION_PERCENT
            logger.info(f"📊 Пирамидинг: {INITIAL_POSITION_PERCENT*100:.0f}% от полного размера")
        
        # POSITION SIZING по AI SCORE: чем выше score, тем больше позиция
        ai_score = analysis.get('score', 70)
        score_multiplier = max(0.5, min(1.2, ai_score / 80))  # от 0.5 до 1.2
        position_size = position_size * score_multiplier
        logger.info(f"📊 AI Score sizing: score={ai_score}, multiplier={score_multiplier:.2f}")
        
        # Проверка минимального размера
        position_value_usdt = position_size * current_price
        if position_value_usdt < MIN_ORDER_SIZE_USD:
            reason = f"Позиция слишком мала: {position_value_usdt:.2f} USDT < {MIN_ORDER_SIZE_USD}"
            logger.warning(f"⚠️ {reason}")
            return None, reason
        
        # Получаем правильное округление для пары
        qty_precision = self._get_qty_precision(pair)
        position_size = round(position_size, qty_precision)
        
        # Размещаем рыночный ордер BUY
        try:
            order_response = self.client.place_order(
                category="spot",
                symbol=pair,
                side="Buy",
                orderType="Market",
                qty=str(position_size),
                marketUnit="baseCoin"
            )
            
            if order_response['retCode'] != 0:
                error_msg = f"Ошибка размещения ордера: {order_response['retMsg']}"
                error_tracker.add_error("Bybit", "OrderError", error_msg)
                logger.error(f"❌ {error_msg}")
                return None, error_msg
            
            order_id = order_response['result']['orderId']
            
            logger.info(f"✅ Ордер размещён: {pair} | {position_size} | ${current_price}")
            
            # Сохраняем ордер в БД
            order_data = {
                'order_id': order_id,
                'pair': pair,
                'side': 'Buy',
                'order_type': 'Market',
                'price': current_price,
                'quantity': position_size,
                'status': 'New',
                'created_at': datetime.now(TIMEZONE).isoformat(),
                'updated_at': datetime.now(TIMEZONE).isoformat()
            }
            
            self.trades_repo.create_order(order_data)
            
            # Создаём позицию
            position_data = {
                'pair': pair,
                'entry_price': current_price,
                'quantity': position_size,
                'tp_price': tp_price,
                'sl_price': sl_price,
                'opened_at': datetime.now(TIMEZONE).isoformat()
            }
            
            position_id = self.trades_repo.create_position(position_data)
            
            # Размещаем TP и SL ордера
            await self._place_tp_sl_orders(pair, position_size, tp_price, sl_price, position_id)
            
            return {
                'position_id': position_id,
                'order_id': order_id,
                'pair': pair,
                'entry_price': current_price,
                'quantity': position_size,
                'tp_price': tp_price,
                'sl_price': sl_price
            }, None
            
        except Exception as e:
            error_tracker.add_error("Bybit", "OrderError", str(e))
            logger.error(f"❌ Ошибка открытия позиции: {e}")
            return None, str(e)
    
    async def _place_tp_sl_orders(self, pair: str, quantity: float, 
                                   tp_price: float, sl_price: float,
                                   position_id: int) -> None:
        """Разместить TP и SL ордера"""
        try:
            # TP ордер (лимитный)
            formatted_qty = self._format_qty(pair, quantity)
            
            tp_response = self.client.place_order(
                category="spot",
                symbol=pair,
                side="Sell",
                orderType="Limit",
                qty=formatted_qty,
                price=self._format_price(pair, tp_price),
                marketUnit="baseCoin"
            )
            
            if tp_response['retCode'] == 0:
                tp_order_id = tp_response['result']['orderId']
                
                self.trades_repo.create_order({
                    'order_id': tp_order_id,
                    'pair': pair,
                    'side': 'Sell',
                    'order_type': 'Limit',
                    'price': tp_price,
                    'quantity': quantity,
                    'status': 'New',
                    'is_tp': True,
                    'position_id': position_id,
                    'created_at': datetime.now(TIMEZONE).isoformat(),
                    'updated_at': datetime.now(TIMEZONE).isoformat()
                })
                
                logger.info(f"✅ TP ордер размещён: {pair} @ {tp_price}")
            
            # SL ордер (рыночный стоп-лосс через triggerPrice)
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
                
                self.trades_repo.create_order({
                    'order_id': sl_order_id,
                    'pair': pair,
                    'side': 'Sell',
                    'order_type': 'Market',
                    'price': sl_price,
                    'quantity': quantity,
                    'status': 'New',
                    'is_sl': True,
                    'position_id': position_id,
                    'created_at': datetime.now(TIMEZONE).isoformat(),
                    'updated_at': datetime.now(TIMEZONE).isoformat()
                })
                
                logger.info(f"✅ SL ордер размещён: {pair} @ {sl_price}")
                
        except Exception as e:
            error_tracker.add_error("Bybit", "TPSLError", str(e))
            logger.error(f"❌ Ошибка размещения TP/SL: {e}")
    
    async def add_to_position(self, position_id: int, analysis: Dict) -> Optional[Dict]:
        """
        Smart DCA: докупить позицию для усреднения
        
        Args:
            position_id: ID позиции для докупки
            analysis: Результат AI анализа
            
        Returns:
            Dict с данными докупки или None
        """
        if not DCA_ENABLED:
            return None
        
        position = self.trades_repo.get_position_by_id(position_id)
        if not position or position['status'] != 'OPEN':
            return None
        
        pair = position['pair']
        dca_count = position.get('dca_count', 0)
        
        # Проверяем лимит докупок
        if dca_count >= DCA_MAX_ENTRIES:
            logger.debug(f"DCA {pair}: достигнут лимит ({DCA_MAX_ENTRIES})")
            return None
        
        # Проверяем AI score
        score = analysis.get('score', 0)
        if score < DCA_MIN_SCORE:
            logger.debug(f"DCA {pair}: score {score} < {DCA_MIN_SCORE}")
            return None
        
        try:
            # Получаем текущую цену
            ticker = self.client.get_tickers(category="spot", symbol=pair)
            current_price = float(ticker['result']['list'][0]['lastPrice'])
            
            # Размер докупки = 50% от начальной позиции
            original_qty = position['quantity']
            dca_qty = original_qty * DCA_POSITION_MULTIPLIER
            
            # Проверяем минимальный размер
            if dca_qty * current_price < MIN_ORDER_SIZE_USD:
                logger.warning(f"DCA {pair}: слишком маленькая докупка")
                return None
            
            # ПРОВЕРКА БАЛАНСА ПЕРЕД ДОКУПКОЙ
            balance = balance_service.get_usdt_balance()
            free_balance = balance.get('free', 0)
            order_cost = dca_qty * current_price
            
            if free_balance < order_cost:
                logger.warning(f"⚠️ DCA {pair}: Недостаточно баланса для докупки. Нужно {order_cost:.2f} USDT, есть {free_balance:.2f} USDT")
                return None

            # Округляем количество для Bybit API (используем правильную точность)
            dca_qty_str = self._format_qty(pair, dca_qty)
            
            # Размещаем ордер
            order_response = self.client.place_order(
                category="spot",
                symbol=pair,
                side="Buy",
                orderType="Market",
                qty=dca_qty_str,
                marketUnit="baseCoin"
            )
            
            if order_response['retCode'] != 0:
                logger.error(f"❌ DCA {pair}: {order_response['retMsg']}")
                return None
            
            order_id = order_response['result']['orderId']
            
            # Расчёт новой средней цены
            old_avg = position.get('avg_entry_price') or position['entry_price']
            old_qty = position['quantity']
            new_qty = old_qty + dca_qty
            new_avg = ((old_avg * old_qty) + (current_price * dca_qty)) / new_qty
            
            # Обновляем позицию
            self._update_position_dca(position_id, new_qty, new_avg, dca_count + 1)
            
            logger.info(f"➕ DCA {pair}: докупка {dca_qty:.6f} @ {current_price:.4f} | Новая средняя: {new_avg:.4f}")
            
            return {
                'order_id': order_id,
                'pair': pair,
                'dca_qty': dca_qty,
                'price': current_price,
                'new_avg': new_avg,
                'new_qty': new_qty,
                'dca_count': dca_count + 1
            }
            
        except Exception as e:
            error_tracker.add_error("Bybit", "DCAError", str(e))
            logger.error(f"❌ Ошибка DCA {pair}: {e}")
            return None
    
    def _update_position_dca(self, position_id: int, new_qty: float, 
                              new_avg: float, dca_count: int) -> None:
        """Обновить позицию после DCA"""
        from ..db.connection import get_transaction
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE positions 
                SET quantity = ?, avg_entry_price = ?, dca_count = ?
                WHERE id = ?
            """, (new_qty, new_avg, dca_count, position_id))
    
    def check_dca_trigger(self, position: Dict) -> bool:
        """Проверить, нужна ли докупка для позиции"""
        if not DCA_ENABLED:
            return False
        
        dca_count = position.get('dca_count', 0)
        if dca_count >= DCA_MAX_ENTRIES:
            return False
        
        entry = position.get('avg_entry_price') or position['entry_price']
        current = position.get('current_price', 0)
        
        if current <= 0:
            return False
        
        # Расчёт просадки
        drawdown = (current - entry) / entry
        
        # ПИРАМИДИНГ: первая докупка при -0.5%, последующие при -1.5%
        if PYRAMIDING_ENABLED and dca_count == 0:
            trigger = PYRAMIDING_TRIGGER  # -0.5%
            logger.debug(f"Пирамидинг триггер: {trigger*100:.1f}%, просадка: {drawdown*100:.2f}%")
        else:
            trigger = DCA_TRIGGER_PERCENT  # -1.5%
        
        return drawdown <= trigger
    
    def _get_qty_precision(self, pair: str) -> int:
        """
        Получить правильное количество десятичных знаков для пары.
        Bybit имеет разные требования для разных монет.
        """
        # Кэш для часто используемых пар (часто используемые значения)
        PRECISION_CACHE = {
            'BTCUSDT': 6,   # BTC: до 0.000001
            'ETHUSDT': 4,   # ETH: до 0.0001
            'SOLUSDT': 2,   # SOL: до 0.01
            'XRPUSDT': 0,   # XRP: целые числа
            'DOGEUSDT': 0,  # DOGE: целые числа
            'ADAUSDT': 0,   # ADA: целые числа
        }
        
        if pair in PRECISION_CACHE:
            return PRECISION_CACHE[pair]
        
        # Для других пар пробуем получить от API
        try:
            response = self.client.get_instruments_info(
                category="spot",
                symbol=pair
            )
            
            if response['retCode'] == 0 and response['result']['list']:
                info = response['result']['list'][0]
                base_precision = info.get('lotSizeFilter', {}).get('basePrecision', '0.01')
                # Считаем количество знаков после запятой
                if '.' in base_precision:
                    decimals = len(base_precision.split('.')[1].rstrip('0'))
                    return decimals
                return 0
        except Exception as e:
            logger.debug(f"Не удалось получить precision для {pair}: {e}")
        
        # По умолчанию - 2 знака (безопаснее)
        return 2

# Глобальный экземпляр
trading_service = TradingService()
