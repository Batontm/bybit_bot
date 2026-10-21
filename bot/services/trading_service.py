"""
Сервис торговли - открытие/закрытие позиций, TP/SL
"""
from typing import Dict, Optional, Tuple, List
from datetime import datetime, timedelta
import asyncio
import math
from pybit.unified_trading import HTTP
from config.settings import logger, TIMEZONE
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, get_pybit_kwargs
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
    DCA_POSITION_MULTIPLIER,
    FUNNEL_LIMIT_TIMEOUT_SEC,
    TRADING_FEE_RATE,
    SLIPPAGE_ESTIMATE,
)
from ..db.trades_repo import TradesRepository
from ..db.pnl_repo import PnLRepository
from .balance_service import balance_service
from .instrument_info import instrument_info
from ..error_tracker import error_tracker


class TradingService:
    """Управление торговыми операциями"""
    
    def __init__(self):
        self.client = HTTP(
            **get_pybit_kwargs(),
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
            recv_window=20000  # Увеличенный timeout
        )
        self.trades_repo = TradesRepository()
        self.pnl_repo = PnLRepository()
    
    def _format_qty(self, pair: str, quantity: float) -> str:
        """Форматировать quantity по шагу инструмента (округление вниз)."""
        return instrument_info.format_qty(pair, quantity)

    def _format_price(self, pair: str, price: float) -> str:
        """Форматировать price по тику инструмента."""
        return instrument_info.format_price(pair, price)
    
    def check_risk_limits(self, strategy: str = "FUNNEL") -> Tuple[bool, str]:
        """
        Проверить все риск-лимиты перед открытием позиции
        Returns: (можно_открывать, причина_если_нет)
        """
        # 0. Защита счёта: серия убытков, просадка от пика
        from .account_guard import account_guard
        allowed, guard_reason = account_guard.entries_allowed()
        if not allowed:
            return False, guard_reason

        # 1–2. Лимиты слотов — отдельно для каждой стратегии (тренд не занимает места воронки)
        open_positions = self.trades_repo.get_open_positions()
        own = [p for p in open_positions if (p.get('strategy') or 'FUNNEL') == strategy]
        if strategy == "TREND":
            from config.trading_config import TREND_MAX_POSITIONS
            if len(own) >= TREND_MAX_POSITIONS:
                return False, f"Достигнут лимит трендовых позиций ({TREND_MAX_POSITIONS})"
        else:
            if len(own) >= MAX_OPEN_POSITIONS:
                return False, f"Достигнут лимит открытых позиций ({MAX_OPEN_POSITIONS})"
            active_pairs = set(pos['pair'] for pos in own)
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
        
        # 5. Проверка суммарного риска по открытым позициям.
        # Риск позиции = сколько потеряем, если сработает стоп: (вход − SL) × количество.
        # Раньше здесь суммировался |нереализованный PnL| — у свежей позиции он ≈ 0,
        # поэтому лимит фактически не работал.
        total_risk = self.open_risk_usdt(open_positions)

        if deposit > 0:
            total_risk_percent = total_risk / deposit
            if total_risk_percent + MAX_RISK_PER_TRADE > MAX_TOTAL_RISK:
                return False, (f"Достигнут лимит суммарного риска: открыто {total_risk_percent*100:.2f}% "
                               f"+ новая сделка {MAX_RISK_PER_TRADE*100:.2f}% > {MAX_TOTAL_RISK*100:.1f}%")

        return True, "Все лимиты в норме"

    @staticmethod
    def open_risk_usdt(open_positions: List[Dict]) -> float:
        """Суммарный риск открытых позиций до их стопов (позиции в безубытке = 0)."""
        total = 0.0
        for pos in open_positions:
            entry = pos.get('avg_entry_price') or pos.get('entry_price') or 0
            sl = pos.get('sl_price') or 0
            qty = pos.get('quantity') or 0
            if entry <= 0 or qty <= 0:
                continue
            if sl <= 0:
                # позиция без стопа — считаем риск по дефолтному SL
                sl = entry * (1 - DEFAULT_SL_PERCENT)
            total += max(0.0, entry - sl) * qty
        return total
    
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
        
        # POSITION SIZING по техническому баллу: слабый сигнал — меньше позиция.
        # Множитель ограничен 1.0: раньше при score > 80 он доходил до 1.2 и риск
        # сделки превышал MAX_RISK_PER_TRADE.
        score = analysis.get('score', 70)
        score_multiplier = max(0.5, min(1.0, score / 80))
        position_size = position_size * score_multiplier
        logger.info(f"📊 Score sizing: score={score}, multiplier={score_multiplier:.2f}")
        
        from .exit_rules import levels
        r_mult = (tp_price - current_price) / (current_price - sl_price) if current_price > sl_price else 2.5
        lv = levels(current_price, sl_price, r_mult)
        return await self._execute_entry(
            pair, position_size, current_price, tp_price, sl_price, limit_price=None,
            tp1_price=lv.partial,
        )

    # ------------------------------------------------------------------ FUNNEL

    async def open_position_with_plan(self, pair: str, plan, *, use_limit: bool = True
                                      ) -> Tuple[Optional[Dict], Optional[str]]:
        """
        Открыть позицию по плану воронки (entry_funnel.TradePlan):
        размер — от стопа за минимумом отката (риск = MAX_RISK_PER_TRADE),
        вход — лимитный post-only по лучшему биду, при неисполнении — отмена.
        """
        can_trade, reason = self.check_risk_limits()
        if not can_trade:
            return None, reason
        can_open, reason = self.can_open_position_for_pair(pair)
        if not can_open:
            return None, reason

        try:
            ticker = self.client.get_tickers(category="spot", symbol=pair)
            row = ticker['result']['list'][0]
            bid = float(row.get('bid1Price') or 0)
            last = float(row['lastPrice'])
        except Exception as e:
            error_tracker.add_error("Bybit", "PriceError", str(e))
            return None, f"Не удалось получить цену {pair}: {e}"

        entry = bid if (use_limit and bid > 0) else last
        if entry <= plan.stop:
            return None, f"Цена {entry} уже ниже стопа {plan.stop:.6g}"

        # Пересчитываем уровни от реальной цены входа, стоп — структурный уровень
        from .exit_rules import levels, position_size as size_with_costs, fee_filter
        r_multiple = (plan.take_profit - plan.entry) / plan.risk_per_unit if plan.risk_per_unit > 0 else 2.5
        lv = levels(entry, plan.stop, r_multiple)
        ok, why = fee_filter(entry, plan.stop, lv.target)
        if not ok:
            return None, f"После пересчёта от цены входа: {why}"

        # Размер: при срабатывании стопа теряем risk_fraction капитала ВКЛЮЧАЯ комиссии
        risk_fraction, risk_note = self.risk_fraction()
        position_size = size_with_costs(balance_service.get_deposit_usdt(), risk_fraction, entry, plan.stop)
        if position_size <= 0:
            return None, "Не удалось рассчитать размер позиции"
        if risk_note:
            logger.info(f"📊 {pair}: {risk_note}")

        # Не тратим больше свободного USDT
        try:
            free = float(balance_service.get_usdt_balance().get('free', 0))
            if free > 0 and position_size * entry > free * 0.98:
                position_size = free * 0.98 / entry
                logger.info(f"📊 {pair}: размер урезан до свободного баланса ({free:.2f} USDT)")
        except Exception:
            pass

        return await self._execute_entry(
            pair, position_size, entry, lv.target, plan.stop,
            limit_price=entry if use_limit and bid > 0 else None,
            tp1_price=lv.partial, entry_atr=getattr(plan, 'atr', None),
        )

    async def open_trend_position(self, pair: str, stop: float, atr: float) -> Tuple[Optional[Dict], Optional[str]]:
        """Вход трендовой стратегии: рыночный ордер, стоп за 2×ATR дневной, тейка нет."""
        from config.trading_config import TREND_RISK_PER_TRADE
        from .exit_rules import position_size as size_with_costs
        can_trade, reason = self.check_risk_limits(strategy="TREND")
        if not can_trade:
            return None, reason
        can_open, reason = self.can_open_position_for_pair(pair)
        if not can_open:
            return None, reason
        try:
            row = self.client.get_tickers(category="spot", symbol=pair)['result']['list'][0]
            price = float(row.get('ask1Price') or row['lastPrice'])
        except Exception as e:
            return None, f"Не удалось получить цену {pair}: {e}"
        if price <= stop:
            return None, "цена уже ниже стопа"
        qty = size_with_costs(balance_service.get_deposit_usdt(), TREND_RISK_PER_TRADE, price, stop)
        try:
            free = float(balance_service.get_usdt_balance().get('free', 0))
            if free > 0 and qty * price > free * 0.98:
                qty = free * 0.98 / price
        except Exception:
            pass
        return await self._execute_entry(pair, qty, price, 0.0, stop, limit_price=None,
                                         entry_atr=atr, strategy="TREND")

    def risk_fraction(self) -> Tuple[float, str]:
        """
        Доля капитала под риск в сделке. При панике на рынке (ATR BTC 4H выше
        своего среднего) риск уменьшается пропорционально, но не ниже VOL_RISK_MIN_FACTOR.
        """
        from config.trading_config import VOL_RISK_SCALING, VOL_RISK_MIN_FACTOR
        base = MAX_RISK_PER_TRADE
        if not VOL_RISK_SCALING:
            return base, ""
        try:
            from .market_regime_service import market_regime_service
            ratio = market_regime_service.get_status().atr_ratio
        except Exception:
            ratio = None
        if ratio and ratio > 1.0:
            factor = max(VOL_RISK_MIN_FACTOR, 1.0 / ratio)
            return base * factor, f"рынок волатилен (ATR ×{ratio:.2f}) — риск сделки {base * factor * 100:.2f}% вместо {base * 100:.2f}%"
        return base, ""

    async def _execute_entry(self, pair: str, quantity: float, expected_price: float,
                             tp_price: float, sl_price: float, *,
                             limit_price: Optional[float], tp1_price: Optional[float] = None,
                             entry_atr: Optional[float] = None,
                             strategy: str = "FUNNEL") -> Tuple[Optional[Dict], Optional[str]]:
        """
        Разместить ордер на покупку, дождаться исполнения и записать позицию
        по ФАКТИЧЕСКОЙ средней цене и количеству (за вычетом комиссии в монете).
        """
        if quantity * expected_price < MIN_ORDER_SIZE_USD:
            reason = f"Позиция слишком мала: {quantity * expected_price:.2f} USDT < {MIN_ORDER_SIZE_USD}"
            logger.warning(f"⚠️ {reason}")
            return None, reason

        qty_str = self._format_qty(pair, quantity)
        if float(qty_str) <= 0:
            return None, "Количество после округления = 0"

        params = dict(category="spot", symbol=pair, side="Buy", qty=qty_str, marketUnit="baseCoin")
        if limit_price is not None:
            params.update(orderType="Limit", price=self._format_price(pair, limit_price),
                          timeInForce="PostOnly")
            timeout = FUNNEL_LIMIT_TIMEOUT_SEC
        else:
            params.update(orderType="Market")
            timeout = 15

        try:
            resp = self.client.place_order(**params)
        except Exception as e:
            error_tracker.add_error("Bybit", "OrderError", str(e))
            return None, str(e)
        if resp.get('retCode') != 0:
            msg = f"Ошибка размещения ордера: {resp.get('retMsg')}"
            error_tracker.add_error("Bybit", "OrderError", msg)
            logger.error(f"❌ {msg}")
            return None, msg

        order_id = resp['result']['orderId']
        logger.info(f"📨 {pair}: ордер {params['orderType']} {qty_str} @ "
                    f"{params.get('price', 'market')} размещён, ждём исполнения...")

        fill = await self._wait_fill(pair, order_id, timeout)
        filled_qty, avg_price, fee = fill['filled_qty'], fill['avg_price'], fill['fee']

        now = datetime.now(TIMEZONE).isoformat()
        if filled_qty <= 0 or avg_price <= 0:
            self.trades_repo.create_order({
                'order_id': order_id, 'pair': pair, 'side': 'Buy',
                'order_type': params['orderType'], 'price': limit_price or expected_price,
                'quantity': float(qty_str), 'status': fill['status'] or 'Cancelled',
                'created_at': now, 'updated_at': now,
            })
            return None, f"Ордер не исполнился за {timeout}с ({fill['status']})"

        # Комиссия спотовой покупки списывается в базовой монете
        net_qty = filled_qty - fee if 0 < fee < filled_qty * 0.01 else filled_qty
        slippage = (avg_price - expected_price) / expected_price if expected_price else 0.0

        try:
            self.trades_repo.create_order({
                'order_id': order_id, 'pair': pair, 'side': 'Buy',
                'order_type': params['orderType'], 'price': limit_price or expected_price,
                'quantity': float(qty_str), 'filled_quantity': filled_qty,
                'avg_fill_price': avg_price, 'slippage': slippage,
                'status': fill['status'] or 'Filled', 'created_at': now, 'updated_at': now,
            })
        except Exception as e:
            # Монеты уже куплены — запись ордера не должна мешать поставить TP/SL
            logger.error(f"❌ {pair}: не удалось записать ордер {order_id} в БД: {e}")

        if net_qty * avg_price < MIN_ORDER_SIZE_USD:
            logger.warning(f"⚠️ {pair}: исполнено только {net_qty} (< {MIN_ORDER_SIZE_USD} USDT) — "
                           f"слишком мало для TP/SL, остаток останется на балансе")
            return None, "Исполнена слишком малая часть ордера"

        # Стоп не может оказаться выше фактической цены
        if sl_price >= avg_price:
            sl_price = avg_price * (1 - DEFAULT_SL_PERCENT)

        position_id = self.trades_repo.create_position({
            'pair': pair, 'entry_price': avg_price, 'quantity': net_qty,
            'tp_price': tp_price, 'sl_price': sl_price, 'opened_at': now,
            'slippage': slippage, 'commission_paid': fee * avg_price,
            'initial_sl': sl_price, 'entry_atr': entry_atr, 'tp1_price': tp1_price,
            'strategy': strategy,
            'risk_usd': net_qty * (avg_price - sl_price) + fee * avg_price + avg_price * net_qty * (
                TRADING_FEE_RATE + SLIPPAGE_ESTIMATE),
        })
        try:
            from ..db.connection import get_transaction
            with get_transaction() as conn:
                conn.execute("UPDATE orders SET position_id = ? WHERE order_id = ?", (position_id, order_id))
        except Exception:
            pass

        await self._place_tp_sl_orders(pair, net_qty, tp_price, sl_price, position_id, tp1_price=tp1_price)
        logger.info(f"✅ {pair}: позиция #{position_id} {net_qty} @ {avg_price} "
                    f"(ожидали {expected_price}, проскальзывание {slippage*100:+.3f}%)")

        return {
            'position_id': position_id, 'order_id': order_id, 'pair': pair,
            'entry_price': avg_price, 'quantity': net_qty,
            'tp_price': tp_price, 'sl_price': sl_price, 'slippage': slippage,
            'tp1_price': tp1_price,
        }, None

    async def _wait_fill(self, pair: str, order_id: str, timeout: float) -> Dict:
        """
        Опрашивать ордер до исполнения/отмены. По таймауту отменить остаток.
        Returns: {status, filled_qty, avg_price, fee}
        """
        deadline = asyncio.get_running_loop().time() + timeout
        last: Dict = {}
        terminal = {'Filled', 'Cancelled', 'Rejected', 'PartiallyFilledCanceled', 'Deactivated'}
        while True:
            last = self._get_order(pair, order_id) or last
            if last.get('orderStatus') in terminal:
                break
            if asyncio.get_running_loop().time() >= deadline:
                try:
                    self.client.cancel_order(category="spot", symbol=pair, orderId=order_id)
                    logger.info(f"⌛ {pair}: ордер {order_id} не исполнился за {timeout:.0f}с — отменён")
                except Exception as e:
                    logger.debug(f"cancel {order_id}: {e}")
                await asyncio.sleep(1)
                last = self._get_order(pair, order_id) or last
                break
            await asyncio.sleep(2)

        def f(key):
            try:
                return float(last.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        return {
            'status': last.get('orderStatus', ''),
            'filled_qty': f('cumExecQty'),
            'avg_price': f('avgPrice'),
            'fee': f('cumExecFee'),
        }

    def _get_order(self, pair: str, order_id: str) -> Optional[Dict]:
        """Найти ордер среди открытых, затем в истории."""
        for getter in (self.client.get_open_orders, self.client.get_order_history):
            try:
                resp = getter(category="spot", symbol=pair, orderId=order_id)
                if resp.get('retCode') == 0 and resp['result']['list']:
                    return resp['result']['list'][0]
            except Exception as e:
                logger.debug(f"get order {order_id}: {e}")
        return None
    
    async def _place_tp_sl_orders(self, pair: str, quantity: float,
                                   tp_price: float, sl_price: float,
                                   position_id: int, tp1_price: Optional[float] = None) -> None:
        """
        Защита позиции на бирже — только стоп-лосс как условный ордер orderFilter=StopOrder:
        он не блокирует монеты до срабатывания (документация Bybit).

        Тейки (TP1 и TP2) на бирже НЕ ставятся: лимитный тейк блокировал все монеты
        позиции, из-за чего стоп не ставился («Insufficient balance») или не мог
        исполниться. Тейки ведёт бот (position_service.check_take_profits, каждые 10 с);
        на споте без VIP комиссия maker = taker = 0.1%, так что по деньгам разницы нет.
        """
        from .position_service import position_service
        ok, info = await position_service.place_stop_order(pair, quantity, sl_price, position_id)
        if ok:
            logger.info(f"✅ SL {pair} @ {sl_price} (StopOrder); тейки ведёт бот: "
                        f"TP1 {tp1_price or '—'}, TP {tp_price}")
        else:
            error_tracker.add_error("Bybit", "SLError", f"{pair}: {info}")
            logger.error(f"❌ SL {pair}: {info} — повторит Auto-SL через 2 мин, до этого защищает аварийный стоп бота")
    
    def get_add_action(self, position: Dict) -> Optional[str]:
        """
        Нужна ли докупка по позиции. Возвращает "PYRAMIDING", "DCA" или None.
        (Метод вызывается контроллером каждые 2 минуты; до v2.2 его не было,
        задача падала с AttributeError, и докупки не работали вообще.)
        """
        if not (PYRAMIDING_ENABLED or DCA_ENABLED) or position.get('strategy') == 'TREND':
            return None
        if position.get('breakeven_activated'):
            return None
        dca_count = position.get('dca_count') or 0
        entry = position.get('avg_entry_price') or position.get('entry_price') or 0
        current = position.get('current_price') or 0
        if entry <= 0 or current <= 0:
            return None
        drawdown = (current - entry) / entry

        if PYRAMIDING_ENABLED and dca_count == 0:
            return "PYRAMIDING" if drawdown <= PYRAMIDING_TRIGGER else None
        if DCA_ENABLED and dca_count < DCA_MAX_ENTRIES:
            return "DCA" if drawdown <= DCA_TRIGGER_PERCENT else None
        return None

    def check_dca_trigger(self, position: Dict) -> bool:
        """Совместимость: True, если нужна любая докупка."""
        return self.get_add_action(position) is not None

    async def add_to_position(self, position_id: int, analysis: Dict) -> Optional[Dict]:
        """
        Докупка (пирамидинг / Smart DCA). После докупки объём TP и SL ордеров
        увеличивается до нового количества — иначе докупленные монеты остаются без стопа.
        """
        position = self.trades_repo.get_position_by_id(position_id)
        if not position or position['status'] != 'OPEN':
            return None

        action = self.get_add_action(position)
        if not action:
            return None

        pair = position['pair']
        dca_count = position.get('dca_count') or 0

        score = analysis.get('score', 0)
        if score < DCA_MIN_SCORE:
            logger.debug(f"{action} {pair}: score {score} < {DCA_MIN_SCORE}")
            return None

        try:
            ticker = self.client.get_tickers(category="spot", symbol=pair)
            current_price = float(ticker['result']['list'][0]['lastPrice'])
        except Exception as e:
            logger.error(f"❌ {action} {pair}: нет цены ({e})")
            return None

        old_qty = position['quantity']
        if action == "PYRAMIDING":
            add_qty = old_qty * PYRAMIDING_ADD_PERCENT / max(INITIAL_POSITION_PERCENT, 1e-9)
        else:
            add_qty = old_qty * DCA_POSITION_MULTIPLIER

        if add_qty * current_price < MIN_ORDER_SIZE_USD:
            logger.warning(f"{action} {pair}: слишком маленькая докупка")
            return None

        free_balance = balance_service.get_usdt_balance().get('free', 0)
        if free_balance < add_qty * current_price:
            logger.warning(f"⚠️ {action} {pair}: недостаточно USDT ({free_balance:.2f})")
            return None

        try:
            resp = self.client.place_order(
                category="spot", symbol=pair, side="Buy", orderType="Market",
                qty=self._format_qty(pair, add_qty), marketUnit="baseCoin",
            )
            if resp.get('retCode') != 0:
                logger.error(f"❌ {action} {pair}: {resp.get('retMsg')}")
                return None
            order_id = resp['result']['orderId']

            fill = await self._wait_fill(pair, order_id, 15)
            filled, avg_fill, fee = fill['filled_qty'], fill['avg_price'], fill['fee']
            if filled <= 0 or avg_fill <= 0:
                logger.error(f"❌ {action} {pair}: ордер не исполнился ({fill['status']})")
                return None
            net_add = filled - fee if 0 < fee < filled * 0.01 else filled
            slippage = (avg_fill - current_price) / current_price

            old_avg = position.get('avg_entry_price') or position['entry_price']
            new_qty = old_qty + net_add
            new_avg = (old_avg * old_qty + avg_fill * net_add) / new_qty
            self._update_position_dca(position_id, new_qty, new_avg, dca_count + 1)

            now = datetime.now(TIMEZONE).isoformat()
            self.trades_repo.create_order({
                'order_id': order_id, 'pair': pair, 'side': 'Buy', 'order_type': 'Market',
                'price': current_price, 'quantity': add_qty, 'filled_quantity': filled,
                'avg_fill_price': avg_fill, 'slippage': slippage, 'status': fill['status'] or 'Filled',
                'position_id': position_id, 'created_at': now, 'updated_at': now,
            })

            await self._resize_protection(position_id, pair, new_qty)

            logger.info(f"➕ {action} {pair}: +{net_add:.6f} @ {avg_fill:.6g} | средняя {new_avg:.6g}")
            return {
                'action': action, 'order_id': order_id, 'pair': pair,
                'dca_qty': net_add, 'price': avg_fill, 'new_avg': new_avg,
                'new_qty': new_qty, 'dca_count': dca_count + 1, 'slippage': slippage,
            }
        except Exception as e:
            error_tracker.add_error("Bybit", "DCAError", str(e))
            logger.error(f"❌ Ошибка {action} {pair}: {e}")
            return None

    async def _resize_protection(self, position_id: int, pair: str, new_qty: float) -> None:
        """
        Подогнать объёмы защитных ордеров под новое количество позиции:
        SL — на всё количество, тейки (TP1/TP2) — пропорционально прежним долям.
        """
        from ..db.connection import get_db
        with get_db() as conn:
            rows = conn.execute("""
                SELECT order_id, is_tp, is_sl, quantity FROM orders
                WHERE position_id = ? AND (is_tp = 1 OR is_sl = 1)
                  AND status IN ('New', 'PartiallyFilled', 'Untriggered', 'Triggered', 'Active')
            """, (position_id,)).fetchall()
        tp_total = sum(float(r['quantity'] or 0) for r in rows if r['is_tp'])
        for r in rows:
            if r['is_tp'] and tp_total > 0:
                target = new_qty * float(r['quantity'] or 0) / tp_total
            else:
                target = new_qty
            qty_str = self._format_qty(pair, target)
            try:
                resp = self.client.amend_order(category="spot", symbol=pair,
                                               orderId=r['order_id'], qty=qty_str)
                if resp.get('retCode') == 0:
                    from ..db.connection import get_transaction
                    with get_transaction() as conn:
                        conn.execute("UPDATE orders SET quantity = ? WHERE order_id = ?",
                                     (float(qty_str), r['order_id']))
                else:
                    logger.error(f"❌ {pair}: не удалось изменить объём ордера {r['order_id']}: "
                                 f"{resp.get('retMsg')}")
            except Exception as e:
                logger.error(f"❌ {pair}: ошибка изменения объёма ордера {r['order_id']}: {e}")
    
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
    
    def _get_qty_precision(self, pair: str) -> int:
        """Количество десятичных знаков для количества (из get_instruments_info)."""
        return instrument_info.qty_decimals(pair)

# Глобальный экземпляр
trading_service = TradingService()
