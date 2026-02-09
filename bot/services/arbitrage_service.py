"""
Сервис Спот-Фьючерсного арбитража
"""
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import math
from pybit.unified_trading import HTTP
from config.settings import logger, TIMEZONE
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, get_pybit_kwargs
from config.trading_config import (
    ARBITRAGE_MIN_VOLUME_USDT,
    ARBITRAGE_SCAN_LIMIT
)
from ..db.arbitrage_repo import ArbitrageRepository
from ..error_tracker import error_tracker


class ArbitrageService:
    """Спот-Фьючерсный арбитраж на Bybit"""
    
    # Исключаем стейблы и левередж токены (копия из сканера)
    EXCLUDED_TOKENS = [
        'USDC', 'DAI', 'FDUSD', 'USDE', 'EUR', 'USD', 'BUSD', 'USDD', 'TUSD', 'PYUSD',
        '3L', '3S', '2L', '2S', '5L', '5S'
    ]

    # Монеты с высоким collateral ratio (>= 0.9) на Bybit Unified Account.
    # Только эти монеты безопасны для арбитража (спот покрывает маржу шорта).
    SAFE_COLLATERAL_COINS = {
        'BTCUSDT', 'ETHUSDT',
    }
    
    # Минимальный funding rate для открытия (0.01% = 0.0001)
    MIN_FUNDING_RATE = 0.0001
    
    def __init__(self):
        self.client = HTTP(
            **get_pybit_kwargs(),
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
            recv_window=20000
        )
        self.repo = ArbitrageRepository()
        
        # Инициализируем таблицу при первом запуске
        self.repo.init_table()
    
    def _format_qty(self, pair: str, qty: float) -> float:
        """
        Форматировать количество в соответствии с требованиями биржи.
        """
        try:
            # Получаем параметры инструмента для фьючерсов (linear)
            response = self.client.get_instruments_info(
                category="linear",
                symbol=pair
            )
            
            if response['retCode'] == 0 and response['result']['list']:
                info = response['result']['list'][0]
                lot_size = info.get('lotSizeFilter', {})
                min_qty = float(lot_size.get('minOrderQty', 0.001))
                qty_step = float(lot_size.get('qtyStep', 0.001))
                
                # Если меньше минимума — возвращаем минимум
                if qty < min_qty:
                    qty = min_qty
                
                # Округляем до шага вниз
                if qty_step > 0:
                    qty = math.floor(qty / qty_step) * qty_step
                
                # Форматируем число без лишних нулей
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                qty = round(qty, decimals)
                
                logger.debug(f"📐 Арбитраж {pair}: qty={qty}, min={min_qty}, step={qty_step}")
                return qty
                
        except Exception as e:
            logger.warning(f"⚠️ Не удалось получить параметры {pair}: {e}")
        
        # Fallback: простое округление вниз
        if 'BTC' in pair:
            factor = 10 ** 5
            return math.floor(qty * factor) / factor
        elif 'ETH' in pair:
            factor = 10 ** 3
            return math.floor(qty * factor) / factor
        else:
            factor = 10 ** 2
            return math.floor(qty * factor) / factor
    
    def _get_liquid_pairs(self) -> List[str]:
        """
        Получить список ликвидных пар, доступных на Spot и Linear.
        """
        try:
            # 1. Получаем тикеры с Linear (Futures)
            linear_res = self.client.get_tickers(category="linear")
            if linear_res['retCode'] != 0:
                logger.error(f"❌ Ошибка тикеров linear: {linear_res['retMsg']}")
                return ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'] # Fallback
            
            # 2. Получаем тикеры со Spot
            spot_res = self.client.get_tickers(category="spot")
            if spot_res['retCode'] != 0:
                logger.error(f"❌ Ошибка тикеров spot: {spot_res['retMsg']}")
                return ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'] # Fallback
            
            spot_symbols = {t['symbol'] for t in spot_res['result']['list'] if t['symbol'].endswith('USDT')}
            
            candidates = []
            for ticker in linear_res['result']['list']:
                symbol = ticker['symbol']
                
                # Фильтры
                if not symbol.endswith('USDT'): continue
                if symbol not in spot_symbols: continue
                
                # Исключения
                is_excluded = any(token in symbol for token in self.EXCLUDED_TOKENS)
                if is_excluded: continue
                
                try:
                    volume = float(ticker.get('turnover24h', 0)) # Объем в USDT
                    if volume < ARBITRAGE_MIN_VOLUME_USDT: continue
                    
                    candidates.append({
                        'symbol': symbol,
                        'volume': volume
                    })
                except (ValueError, TypeError):
                    continue
            
            # Сортируем по объему и берем лимит
            candidates.sort(key=lambda x: x['volume'], reverse=True)
            liquid_pairs = [c['symbol'] for c in candidates[:ARBITRAGE_SCAN_LIMIT]]
            
            # Гарантируем наличие топов, если их нет в списке
            for top in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']:
                if top not in liquid_pairs and top in spot_symbols:
                    liquid_pairs.append(top)
            
            logger.info(f"🔍 Арбитражный сканер: найдено {len(liquid_pairs)} ликвидных пар")
            return liquid_pairs
            
        except Exception as e:
            logger.error(f"❌ Ошибка в _get_liquid_pairs: {e}")
            return ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']

    def scan_funding_rates(self) -> List[Dict]:
        """
        Сканировать funding rates для всех ликвидных пар.
        
        Returns:
            Список пар с funding rate и APY
        """
        opportunities = []
        scan_pairs = self._get_liquid_pairs()
        
        for pair in scan_pairs:
            try:
                # Получаем funding rate с фьючерсов
                response = self.client.get_tickers(
                    category="linear",
                    symbol=pair
                )
                
                if response['retCode'] == 0 and response['result']['list']:
                    ticker = response['result']['list'][0]
                    funding_rate = float(ticker.get('fundingRate', 0))
                    next_funding_time = ticker.get('nextFundingTime', '')
                    mark_price = float(ticker.get('markPrice', 0))
                    
                    # Расчёт APY (funding каждые 8ч = 3 раза в день = 1095 раз в год)
                    apy = funding_rate * 3 * 365 * 100  # в процентах
                    
                    # Определяем риск
                    if funding_rate >= 0.0003:
                        risk = "🟢"  # Низкий риск, высокий доход
                    elif funding_rate >= 0.0001:
                        risk = "🟡"  # Средний
                    else:
                        risk = "🔴"  # Низкий доход или отрицательный
                    
                    opportunities.append({
                        'pair': pair,
                        'funding_rate': funding_rate,
                        'funding_pct': funding_rate * 100,
                        'apy': apy,
                        'mark_price': mark_price,
                        'next_funding': next_funding_time,
                        'risk': risk
                    })
                    
            except Exception as e:
                logger.error(f"❌ Ошибка получения funding {pair}: {e}")
        
        # Сортируем по funding rate (от большего к меньшему)
        opportunities.sort(key=lambda x: x['funding_rate'], reverse=True)
        
        return opportunities
    
    async def open_arbitrage(self, pair: str, amount_usdt: float) -> Tuple[bool, str]:
        """
        Открыть арбитражную позицию: LONG спот + SHORT фьючерс
        
        Args:
            pair: Торговая пара (BTCUSDT)
            amount_usdt: Сумма в USDT
            
        Returns:
            (success, message)
        """
        try:
            # Проверка collateral whitelist — только BTC/ETH безопасны для арбитража
            if pair not in self.SAFE_COLLATERAL_COINS:
                return False, f"⚠️ {pair} не в whitelist арбитража (риск ликвидации, collateral ratio < 0.9)"

            # Проверяем, нет ли уже открытой позиции по этой паре
            existing = self.repo.get_position_by_pair(pair)
            if existing:
                return False, f"Уже есть открытая арбитражная позиция по {pair}"
            
            # Получаем текущую цену
            spot_ticker = self.client.get_tickers(category="spot", symbol=pair)
            if spot_ticker['retCode'] != 0:
                return False, f"Не удалось получить цену {pair}"
            
            current_price = float(spot_ticker['result']['list'][0]['lastPrice'])
            raw_qty = amount_usdt / current_price
            
            # Получаем параметры инструмента для правильного округления
            qty = self._format_qty(pair, raw_qty)
            
            if qty <= 0:
                return False, f"Слишком маленький размер позиции для {pair}"
            
            # 1. Покупаем на СПОТ
            spot_response = self.client.place_order(
                category="spot",
                symbol=pair,
                side="Buy",
                orderType="Market",
                qty=str(qty),
                marketUnit="baseCoin"
            )
            
            if spot_response['retCode'] != 0:
                return False, f"Ошибка спот ордера: {spot_response['retMsg']}"
            
            spot_order_id = spot_response['result']['orderId']
            logger.info(f"✅ СПОТ куплено: {qty} {pair} @ {current_price}")
            
            # 2. Открываем SHORT на фьючерсах
            futures_response = self.client.place_order(
                category="linear",
                symbol=pair,
                side="Sell",
                orderType="Market",
                qty=str(qty)
            )
            
            if futures_response['retCode'] != 0:
                # Если фьючерс не открылся — продаём спот обратно
                logger.error(f"❌ Ошибка фьючерса: {futures_response['retMsg']}")
                self.client.place_order(
                    category="spot",
                    symbol=pair,
                    side="Sell",
                    orderType="Market",
                    qty=str(qty),
                    marketUnit="baseCoin"
                )
                return False, f"Ошибка фьючерса: {futures_response['retMsg']}"
            
            futures_order_id = futures_response['result']['orderId']
            logger.info(f"✅ SHORT открыт: {qty} {pair} @ {current_price}")
            
            # 3. Сохраняем в БД
            position_id = self.repo.create_position({
                'pair': pair,
                'spot_order_id': spot_order_id,
                'futures_order_id': futures_order_id,
                'spot_qty': qty,
                'futures_qty': qty,
                'entry_price': current_price
            })
            
            return True, f"✅ Арбитраж открыт: {pair} | {qty} @ ${current_price:.2f} | ID: {position_id}"
            
        except Exception as e:
            error_tracker.add_error("Arbitrage", "OpenError", str(e))
            logger.error(f"❌ Ошибка открытия арбитража: {e}")
            return False, f"Ошибка: {str(e)}"
    
    async def close_arbitrage(self, position_id: int) -> Tuple[bool, str]:
        """
        Закрыть арбитражную позицию
        
        Args:
            position_id: ID позиции в БД
            
        Returns:
            (success, message)
        """
        try:
            position = self.repo.get_position_by_id(position_id)
            if not position:
                return False, f"Позиция #{position_id} не найдена"
            
            if position['status'] != 'OPEN':
                return False, f"Позиция #{position_id} уже закрыта"
            
            pair = position['pair']
            qty = position['spot_qty']
            entry_price = position['entry_price']
            
            # 1. Продаём спот
            spot_response = self.client.place_order(
                category="spot",
                symbol=pair,
                side="Sell",
                orderType="Market",
                qty=str(qty),
                marketUnit="baseCoin"
            )
            
            if spot_response['retCode'] != 0:
                return False, f"Ошибка продажи спота: {spot_response['retMsg']}"
            
            logger.info(f"✅ СПОТ продан: {qty} {pair}")
            
            # 2. Закрываем SHORT (Buy для закрытия)
            futures_response = self.client.place_order(
                category="linear",
                symbol=pair,
                side="Buy",
                orderType="Market",
                qty=str(qty),
                reduceOnly=True
            )
            
            if futures_response['retCode'] != 0:
                logger.error(f"⚠️ Ошибка закрытия фьючерса: {futures_response['retMsg']}")
            else:
                logger.info(f"✅ SHORT закрыт: {qty} {pair}")
            
            # 3. Получаем текущую цену для расчёта PnL
            ticker = self.client.get_tickers(category="spot", symbol=pair)
            exit_price = float(ticker['result']['list'][0]['lastPrice']) if ticker['retCode'] == 0 else entry_price
            
            # Расчёт PnL: накопленный funding + (мизерная разница спот/фьючерс)
            # В идеальном арбитраже спот и фьючерс компенсируют друг друга
            accumulated_funding = position['accumulated_funding']
            realized_pnl = accumulated_funding  # Основная прибыль — от funding
            
            # 4. Закрываем в БД
            self.repo.close_position(position_id, realized_pnl)
            
            return True, f"✅ Арбитраж закрыт: {pair} | Funding PnL: ${realized_pnl:.2f}"
            
        except Exception as e:
            error_tracker.add_error("Arbitrage", "CloseError", str(e))
            logger.error(f"❌ Ошибка закрытия арбитража: {e}")
            return False, f"Ошибка: {str(e)}"
    
    async def close_all_arbitrages(self) -> Tuple[int, str]:
        """Закрыть все открытые арбитражные позиции"""
        positions = self.repo.get_open_positions()
        closed = 0
        errors = []
        
        for pos in positions:
            success, msg = await self.close_arbitrage(pos['id'])
            if success:
                closed += 1
            else:
                errors.append(msg)
        
        if errors:
            return closed, f"Закрыто {closed}, ошибки: {'; '.join(errors)}"
        return closed, f"✅ Закрыто {closed} арбитражных позиций"
    
    def update_funding_for_all(self) -> None:
        """
        Обновить накопленный funding для всех открытых позиций.
        Вызывается каждые 8 часов по времени funding.
        """
        positions = self.repo.get_open_positions()
        
        for pos in positions:
            try:
                pair = pos['pair']
                qty = pos['spot_qty']
                
                # Получаем текущий funding rate
                response = self.client.get_tickers(category="linear", symbol=pair)
                if response['retCode'] == 0 and response['result']['list']:
                    funding_rate = float(response['result']['list'][0].get('fundingRate', 0))
                    mark_price = float(response['result']['list'][0].get('markPrice', 0))
                    
                    # Рассчитываем funding payment
                    # Для SHORT позиции: если funding положительный — мы получаем деньги
                    funding_payment = funding_rate * qty * mark_price
                    
                    # Обновляем в БД
                    self.repo.update_funding(pos['id'], funding_payment)
                    logger.info(f"💰 Funding: {pair} | +${funding_payment:.4f}")
                    
            except Exception as e:
                logger.error(f"❌ Ошибка обновления funding {pos['pair']}: {e}")
    
    def get_dashboard(self) -> Dict:
        """
        Получить данные для dashboard арбитража.
        
        Returns:
            Dict с позициями и статистикой
        """
        positions = self.repo.get_open_positions()
        stats = self.repo.get_statistics()
        
        # Рассчитываем общий APY
        total_value = sum(p['entry_price'] * p['spot_qty'] for p in positions)
        total_funding = stats.get('open_funding', 0)
        
        # Примерный APY (упрощённый расчёт)
        if total_value > 0 and positions:
            first_pos = min(positions, key=lambda x: x['opened_at'])
            # Дней с открытия
            opened_at = datetime.fromisoformat(first_pos['opened_at'].replace('Z', '+00:00'))
            days_open = max(1, (datetime.now(TIMEZONE) - opened_at).days)
            daily_return = total_funding / days_open
            apy = (daily_return / total_value) * 365 * 100 if total_value else 0
        else:
            apy = 0
        
        return {
            'positions': positions,
            'stats': stats,
            'total_funding': total_funding,
            'apy': apy,
            'total_value': total_value
        }


# Глобальный экземпляр
arbitrage_service = ArbitrageService()
