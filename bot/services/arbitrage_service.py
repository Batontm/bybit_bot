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
    ARBITRAGE_SCAN_LIMIT,
    ARBITRAGE_MIN_FUNDING_RATE,
    ARBITRAGE_FEE_ROUNDTRIP,
    ARBITRAGE_MAX_BREAKEVEN_DAYS,
    ARBITRAGE_EXIT_NEGATIVE_PERIODS,
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
    
    # Минимальный funding rate для открытия: окупить комиссии за ARBITRAGE_MAX_BREAKEVEN_DAYS
    MIN_FUNDING_RATE = max(ARBITRAGE_MIN_FUNDING_RATE,
                           ARBITRAGE_FEE_ROUNDTRIP / (3 * ARBITRAGE_MAX_BREAKEVEN_DAYS))
    
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

    @classmethod
    def evaluate_funding(cls, pair: str, funding_rate: float) -> Dict:
        """Окупаемость и допуск к входу для ставки funding (за 8 часов)."""
        breakeven_days = (ARBITRAGE_FEE_ROUNDTRIP / (funding_rate * 3)) if funding_rate > 0 else None
        whitelisted = pair in cls.SAFE_COLLATERAL_COINS
        entry_ok = whitelisted and funding_rate >= cls.MIN_FUNDING_RATE
        if not whitelisted:
            reason = "не в whitelist (только BTC/ETH)"
        elif funding_rate <= 0:
            reason = "funding ≤ 0 — шорт платит"
        elif not entry_ok:
            reason = f"окупится за {breakeven_days:.1f} дн. > {ARBITRAGE_MAX_BREAKEVEN_DAYS:g}"
        else:
            reason = f"окупится за {breakeven_days:.1f} дн."
        return {'breakeven_days': breakeven_days, 'whitelisted': whitelisted,
                'entry_ok': entry_ok, 'reason': reason}

    def scan_funding_rates(self, pairs: Optional[List[str]] = None) -> List[Dict]:
        """
        Сканировать funding rates.

        Args:
            pairs: только эти пары (авто-арбитраж сканирует только whitelist);
                   None — топ ликвидных пар (ручной анализ в Telegram).
        """
        opportunities = []
        scan_pairs = pairs if pairs is not None else self._get_liquid_pairs()
        
        for pair in scan_pairs:
            try:
                response = self.client.get_tickers(category="linear", symbol=pair)
                if response['retCode'] == 0 and response['result']['list']:
                    ticker = response['result']['list'][0]
                    funding_rate = float(ticker.get('fundingRate') or 0)
                    mark_price = float(ticker.get('markPrice') or 0)
                    apy = funding_rate * 3 * 365 * 100  # в процентах, без учёта комиссий
                    ev = self.evaluate_funding(pair, funding_rate)
                    risk = "🟢" if ev['entry_ok'] else ("🟡" if funding_rate > 0 else "🔴")
                    opportunities.append({
                        'pair': pair,
                        'funding_rate': funding_rate,
                        'funding_pct': funding_rate * 100,
                        'apy': apy,
                        'mark_price': mark_price,
                        'next_funding': ticker.get('nextFundingTime', ''),
                        'risk': risk,
                        **ev,
                    })
            except Exception as e:
                logger.error(f"❌ Ошибка получения funding {pair}: {e}")
        
        opportunities.sort(key=lambda x: x['funding_rate'], reverse=True)
        return opportunities
    
    def _spot_fill(self, pair: str, order_id: str) -> Tuple[float, float, float]:
        """(исполнено, средняя цена, комиссия в монете) для спотового ордера."""
        import time as _t
        for _ in range(8):
            try:
                r = self.client.get_order_history(category="spot", symbol=pair, orderId=order_id)
                items = (r.get('result') or {}).get('list') or []
                if r.get('retCode') == 0 and items and items[0].get('orderStatus') in (
                        'Filled', 'PartiallyFilledCanceled', 'Cancelled', 'Rejected'):
                    it = items[0]
                    return (float(it.get('cumExecQty') or 0), float(it.get('avgPrice') or 0),
                            float(it.get('cumExecFee') or 0))
            except Exception as e:
                logger.debug(f"spot fill {order_id}: {e}")
            _t.sleep(1)
        return 0.0, 0.0, 0.0

    def _coin_balance(self, pair: str) -> Optional[float]:
        """Свободный остаток базовой монеты на UNIFIED (None — не удалось узнать)."""
        coin = pair[:-4]
        try:
            r = self.client.get_wallet_balance(accountType="UNIFIED", coin=coin)
            for c in ((r.get('result') or {}).get('list') or [{}])[0].get('coin', []):
                if c.get('coin') == coin:
                    wb = float(c.get('walletBalance') or 0)
                    locked = float(c.get('locked') or 0)
                    return max(0.0, wb - locked)
            return 0.0
        except Exception:
            return None

    async def open_arbitrage(self, pair: str, amount_usdt: float) -> Tuple[bool, str]:
        """
        Открыть арбитраж: LONG спот + SHORT перп на то же количество.
        Количество шорта берётся от ФАКТИЧЕСКИ полученных монет (за вычетом комиссии).
        """
        try:
            if pair not in self.SAFE_COLLATERAL_COINS:
                return False, f"⚠️ {pair} не в whitelist арбитража (риск ликвидации, collateral ratio < 0.9)"

            if self.repo.get_position_by_pair(pair):
                return False, f"Уже есть открытая арбитражная позиция по {pair}"

            # Не торгуем монету, которую держит воронка: позиции продавали бы монеты друг друга
            from ..db.trades_repo import TradesRepository
            if TradesRepository.get_open_positions(pair=pair):
                return False, f"По {pair} открыта обычная позиция бота — арбитраж не открываем"

            spot_ticker = self.client.get_tickers(category="spot", symbol=pair)
            if spot_ticker['retCode'] != 0:
                return False, f"Не удалось получить цену {pair}"
            current_price = float(spot_ticker['result']['list'][0]['lastPrice'])

            from .instrument_info import instrument_info
            qty = self._format_qty(pair, amount_usdt / current_price)  # шаг перпа (крупнее спотового)
            if qty <= 0:
                return False, f"Слишком маленький размер позиции для {pair}"

            # 1. Спот
            spot_response = self.client.place_order(
                category="spot", symbol=pair, side="Buy", orderType="Market",
                qty=instrument_info.format_qty(pair, qty), marketUnit="baseCoin",
            )
            if spot_response['retCode'] != 0:
                return False, f"Ошибка спот ордера: {spot_response['retMsg']}"
            spot_order_id = spot_response['result']['orderId']

            filled, avg_price, fee = self._spot_fill(pair, spot_order_id)
            if filled <= 0:
                return False, f"Спот-ордер {pair} не исполнился — арбитраж не открыт"
            net_spot = filled - fee if 0 < fee < filled * 0.01 else filled
            fut_qty = self._format_qty(pair, net_spot)
            if fut_qty <= 0 or fut_qty > net_spot + 1e-12:
                fut_qty = self._format_qty(pair, net_spot * 0.999)
            logger.info(f"✅ СПОТ куплено: {net_spot} {pair} @ {avg_price}")

            # 2. Шорт
            futures_response = self.client.place_order(
                category="linear", symbol=pair, side="Sell", orderType="Market", qty=str(fut_qty),
            )
            if futures_response['retCode'] != 0:
                logger.error(f"❌ Ошибка фьючерса: {futures_response['retMsg']}")
                self.client.place_order(
                    category="spot", symbol=pair, side="Sell", orderType="Market",
                    qty=instrument_info.format_qty(pair, net_spot), marketUnit="baseCoin",
                )
                return False, f"Ошибка фьючерса: {futures_response['retMsg']} — спот продан обратно"

            position_id = self.repo.create_position({
                'pair': pair,
                'spot_order_id': spot_order_id,
                'futures_order_id': futures_response['result']['orderId'],
                'spot_qty': net_spot,
                'futures_qty': fut_qty,
                'entry_price': avg_price or current_price,
            })
            ev = self.evaluate_funding(pair, self._current_funding(pair) or 0.0)
            return True, (f"✅ Арбитраж открыт: {pair} | {fut_qty} @ ${avg_price or current_price:,.2f} "
                          f"| ID: {position_id} | {ev['reason']}")

        except Exception as e:
            error_tracker.add_error("Arbitrage", "OpenError", str(e))
            logger.error(f"❌ Ошибка открытия арбитража: {e}")
            return False, f"Ошибка: {str(e)}"

    def _current_funding(self, pair: str) -> Optional[float]:
        try:
            r = self.client.get_tickers(category="linear", symbol=pair)
            return float(r['result']['list'][0].get('fundingRate') or 0)
        except Exception:
            return None
    
    async def close_arbitrage(self, position_id: int, reason: str = "manual") -> Tuple[bool, str]:
        """
        Закрыть арбитраж: продать спот (не больше реального остатка) и выкупить шорт.
        Обе ноги закрываются независимо: ошибка одной не оставляет вторую без попытки.
        PnL = накопленный funding − оценка комиссий полного круга.
        """
        try:
            position = self.repo.get_position_by_id(position_id)
            if not position:
                return False, f"Позиция #{position_id} не найдена"
            if position['status'] != 'OPEN':
                return False, f"Позиция #{position_id} уже закрыта"

            from .instrument_info import instrument_info
            pair = position['pair']
            spot_qty = float(position['spot_qty'])
            fut_qty = float(position['futures_qty'])
            entry_price = float(position['entry_price'])
            problems = []

            # 1. Спот — продаём не больше, чем реально есть (комиссия покупки списана в монете)
            balance = self._coin_balance(pair)
            sell_qty = min(spot_qty, balance) if balance is not None else spot_qty
            spot_response = self.client.place_order(
                category="spot", symbol=pair, side="Sell", orderType="Market",
                qty=instrument_info.format_qty(pair, sell_qty), marketUnit="baseCoin",
            )
            if spot_response.get('retCode') != 0:
                problems.append(f"спот: {spot_response.get('retMsg')}")

            # 2. Шорт
            futures_response = self.client.place_order(
                category="linear", symbol=pair, side="Buy", orderType="Market",
                qty=str(fut_qty), reduceOnly=True,
            )
            if futures_response.get('retCode') != 0:
                problems.append(f"фьючерс: {futures_response.get('retMsg')}")

            if problems:
                note = "Закрытие не завершено: " + "; ".join(problems)
                self.repo.set_note(position_id, note)
                return False, f"⚠️ {pair}: {note}. Проверьте позицию на бирже вручную."

            fees = ARBITRAGE_FEE_ROUNDTRIP * entry_price * fut_qty
            funding = float(position.get('accumulated_funding') or 0)
            realized_pnl = funding - fees
            self.repo.close_position(position_id, realized_pnl)
            sign = "+" if realized_pnl >= 0 else "-"
            return True, (f"✅ Арбитраж закрыт ({reason}): {pair} | funding ${funding:.2f} − комиссии "
                          f"≈${fees:.2f} = {sign}${abs(realized_pnl):.2f}")

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
    
    def _settled_funding(self, pair: str, limit: int = 10) -> List[Tuple[int, float]]:
        """Фактически начисленные ставки [(timestamp_ms, rate)] от старых к новым."""
        r = self.client.get_funding_rate_history(category="linear", symbol=pair, limit=limit)
        if r.get('retCode') != 0:
            return []
        rows = [(int(x.get('fundingRateTimestamp') or 0), float(x.get('fundingRate') or 0))
                for x in (r.get('result') or {}).get('list') or []]
        return sorted(rows)

    async def settle_funding(self) -> List[str]:
        """
        Учесть фактически начисленный funding по открытым позициям и закрыть те,
        где последние ARBITRAGE_EXIT_NEGATIVE_PERIODS начислений отрицательные.
        Returns: сообщения для уведомлений.
        """
        messages: List[str] = []
        for pos in self.repo.get_open_positions():
            pair = pos['pair']
            try:
                history = self._settled_funding(pair)
                if not history:
                    continue
                opened_ms = int(datetime.fromisoformat(pos['opened_at']).timestamp() * 1000)
                last_ts = int(pos.get('last_funding_ts') or 0)
                fresh = [(ts, rate) for ts, rate in history if ts > max(last_ts, opened_ms)]
                if fresh:
                    mark = float(self.client.get_tickers(category="linear", symbol=pair)
                                 ['result']['list'][0].get('markPrice') or pos['entry_price'])
                    # SHORT получает funding при положительной ставке и платит при отрицательной
                    amount = sum(rate for _, rate in fresh) * float(pos['futures_qty']) * mark
                    self.repo.add_settled_funding(pos['id'], amount, len(fresh), fresh[-1][0])
                    logger.info(f"💰 Funding {pair}: {len(fresh)} начисл. = {amount:+.4f} USDT")

                # выход при устойчиво отрицательном funding (считаем только начисления после входа)
                after_open = [rate for ts, rate in history if ts > opened_ms]
                n = ARBITRAGE_EXIT_NEGATIVE_PERIODS
                if len(after_open) >= n and all(rate < 0 for rate in after_open[-n:]):
                    ok, msg = await self.close_arbitrage(pos['id'], reason=f"funding < 0 {n} раза подряд")
                    messages.append(("💹 " if ok else "⚠️ ") + msg)
            except Exception as e:
                logger.error(f"❌ Ошибка учёта funding {pair}: {e}")
        return messages

    def update_funding_for_all(self) -> None:
        """Совместимость: учесть начисленный funding (без авто-выхода)."""
        import asyncio
        try:
            asyncio.run(self.settle_funding())
        except RuntimeError:
            logger.warning("update_funding_for_all вызван внутри event loop — используйте settle_funding()")

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
