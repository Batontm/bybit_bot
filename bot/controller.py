"""
Главный контроллер бота - управление циклами и планировщиком
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config.settings import logger, TIMEZONE
from config.trading_config import (
    DEFAULT_TRADING_PAIRS,
    MAX_CONSECUTIVE_LOSSES,
    ARBITRAGE_MIN_FUNDING_RATE,
    ARBITRAGE_MAX_POSITIONS,
    ARBITRAGE_POSITION_SIZE_USD,
    ARBITRAGE_RED_MODE_ENABLED,
    ARBITRAGE_RED_MAX_POSITIONS,
    ARBITRAGE_RED_TOTAL_ALLOCATION_PCT,
    ARBITRAGE_RED_MIN_POSITION_SIZE_USD,
    MAX_SLIPPAGE_PERCENT,
    ENTRY_STRATEGY,
)
from .db.connection import db
from .services.analysis_service import analysis_service
from .services.trading_service import trading_service
from .services.position_service import position_service
from .services.balance_service import balance_service
from .services.scanner_service import scanner_service
from .services.websocket_service import websocket_service
from .services.prefilter_service import prefilter_service
from .services.arbitrage_service import arbitrage_service
from .services.market_regime_service import market_regime_service
from .services.entry_funnel_service import entry_funnel_service
from .services.notification_dispatcher import NotificationDispatcher
from .services.slippage_guard import SlippageGuard
from .services.job_scheduler import setup_jobs


BOT_VERSION = "v2.5.0"


class BotController:
    """Главный контроллер и планировщик задач"""
    
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        self.mode = "ACTIVE"  # ACTIVE, RISK_ONLY, PAUSED
        self.pause_reason: str = ""  # panic / manual — пауза снимается только вручную
        self.last_signal_check: Dict | None = None
        self.auto_trading_enabled = True
        self.scanner_enabled = False  # По умолчанию выключен
        self.started_at: datetime = datetime.now(TIMEZONE)

        # Подсистемы
        self.notifier = NotificationDispatcher()
        self.slippage_guard = SlippageGuard(notifier=self._send_notification)

        # Все операции, которые торгуют и меняют позиции, выполняются по очереди
        # в одном отдельном потоке: сетевые запросы к Bybit (до 10 с на ответ)
        # больше не блокируют Telegram и планировщик, а задачи не мешают друг другу.
        self._exchange_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="exchange")
        self._main_loop: asyncio.AbstractEventLoop | None = None

        # Сервисы
        self.trading_service = trading_service
        self.position_service = position_service
        self.analysis_service = analysis_service
        self.balance_service = balance_service
        self.scanner_service = scanner_service

        self.fixed_pairs = list(DEFAULT_TRADING_PAIRS)
        self.dynamic_pairs = []
        self.dynamic_pairs_data = {}  # {symbol: timestamp} для 24ч памяти

        self.is_running = False

    # -------- backward-compat: флаги уведомлений делегируем в dispatcher --------

    @property
    def notifications_enabled(self) -> bool:
        return self.notifier.notifications_enabled

    @notifications_enabled.setter
    def notifications_enabled(self, value: bool) -> None:
        self.notifier.notifications_enabled = bool(value)

    @property
    def alerts_enabled(self) -> bool:
        return self.notifier.alerts_enabled

    @alerts_enabled.setter
    def alerts_enabled(self, value: bool) -> None:
        self.notifier.alerts_enabled = bool(value)

    @property
    def notification_callback(self):
        return self.notifier._callback

    @notification_callback.setter
    def notification_callback(self, value) -> None:
        self.notifier._callback = value
    
    def set_notifier(self, callback):
        """Установить callback для уведомлений"""
        self.notification_callback = callback

    # -------- thin delegates (sane backward-compat) --------

    async def _send_notification(self, message: str) -> None:
        """Отправить уведомление из любого потока: Telegram живёт в основном цикле."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._main_loop is not None and loop is not self._main_loop and self._main_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self.notifier.send(message), self._main_loop)
            await asyncio.wrap_future(fut)
        else:
            await self.notifier.send(message)

    # -------- фоновое выполнение --------

    # Задачи, которые только читают рынок и сами вызывают run_exchange для сделок
    _THREAD_JOBS = frozenset({'_check_signals'})

    @staticmethod
    def _in_exchange_worker() -> bool:
        return threading.current_thread().name.startswith("exchange")

    async def run_exchange(self, coro_fn, *args, **kwargs):
        """
        Выполнить корутину сервиса в потоке биржи (по очереди с остальными
        торговыми задачами). Внутри самого потока — выполнить сразу (без дедлока).
        """
        if self._in_exchange_worker():
            return await coro_fn(*args, **kwargs)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._exchange_pool, lambda: asyncio.run(coro_fn(*args, **kwargs)))

    async def run_job(self, method_name: str) -> None:
        """Точка входа планировщика: задача уходит в фоновый поток, цикл остаётся свободным."""
        fn = getattr(self, method_name)
        t0 = time.monotonic()
        try:
            if method_name in self._THREAD_JOBS:
                await asyncio.to_thread(lambda: asyncio.run(fn()))
            else:
                await self.run_exchange(fn)
        except Exception as e:
            logger.error(f"❌ Задача {method_name}: {e}")
        took = time.monotonic() - t0
        if took > 20:
            logger.warning(f"🐢 Задача {method_name} выполнялась {took:.0f} с")

    @staticmethod
    def _is_alert_message(message: str) -> bool:
        return NotificationDispatcher.is_alert(message)

    def _ban_key(self, pair: str) -> str:
        return SlippageGuard._ban_key(pair)

    def _is_pair_banned(self, pair: str) -> bool:
        return self.slippage_guard.is_pair_banned(pair)

    def _record_slippage_event(self, pair: str, action: str, slippage: float) -> int:
        return self.slippage_guard.record_event(pair, action, slippage)

    async def _ban_pair_for_slippage(self, pair: str, exceed_count_24h: int) -> None:
        await self.slippage_guard._ban_pair(pair, exceed_count_24h)
    
    async def start(self):
        """Запустить контроллер и планировщик"""
        logger.info("🚀 Запуск контроллера...")
        
        self._main_loop = asyncio.get_running_loop()

        # Получаем настройки сканера из БД
        self._load_settings()
        
        # Запуск WebSocket
        active_pairs = self.get_active_pairs()
        websocket_service.subscribe(active_pairs)
        
        # Настройка задач планировщика
        self._setup_scheduler()
        
        # Запуск планировщика
        self.scheduler.start()
        self.is_running = True
        
        # Сразу запускаем сканер если он включен
        if self.scanner_enabled:
            asyncio.create_task(self.run_job('_update_market_pairs'))
            
        logger.info("✅ Контроллер запущен")
    
    async def stop(self):
        """Остановить контроллер"""
        logger.info("🛑 Остановка контроллера...")
        
        self.is_running = False
        
        # Проверяем, что scheduler запущен перед остановкой
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        # Текущая задача в потоке биржи доработает сама, очередь отбрасываем
        self._exchange_pool.shutdown(wait=False, cancel_futures=True)
        
        # Закрываем подключение к БД
        db.close()
        
        logger.info("✅ Контроллер остановлен")

    async def panic_sell_all(self) -> List[str]:
        """
        🚨 PANIC SELL: экстренная остановка.
        1. Отменить ВСЕ ордера на бирже
        2. Закрыть ВСЕ открытые позиции по рынку
        3. Закрыть ВСЕ арбитражные позиции
        4. Поставить бота на PAUSE
        Returns: Список сообщений о действиях
        """
        messages = ["🚨 PANIC SELL запущен!"]
        logger.warning("🚨 PANIC SELL: экстренная остановка")

        # 1. Пауза бота
        self.mode = "PAUSED"
        self.pause_reason = "panic"
        self.auto_trading_enabled = False
        messages.append("⏸️ Бот переведён в PAUSED")

        # 2. Отмена всех ордеров на бирже
        # обычные ордера и условные (SL с triggerPrice) отменяются отдельными запросами
        for extra, label in (({}, "спот-ордера"), ({"orderFilter": "StopOrder"}, "условные спот-ордера"),
                             ({"orderFilter": "tpslOrder"}, "TP/SL спот-ордера")):
            try:
                resp = self.position_service.client.cancel_all_orders(category="spot", **extra)
                if resp.get('retCode') == 0:
                    messages.append(f"✅ Отменены: {label}")
                else:
                    messages.append(f"⚠️ Отмена ({label}): {resp.get('retMsg', '?')}")
            except Exception as e:
                messages.append(f"❌ Ошибка отмены ({label}): {e}")

        try:
            resp = self.position_service.client.cancel_all_orders(category="linear")
            if resp.get('retCode') == 0:
                messages.append("✅ Все фьючерс-ордера отменены")
            else:
                messages.append(f"⚠️ Отмена фьючерс-ордеров: {resp.get('retMsg', '?')}")
        except Exception as e:
            messages.append(f"❌ Ошибка отмены фьючерс-ордеров: {e}")

        # 3. Закрытие всех открытых позиций (спот)
        open_positions = self.position_service.trades_repo.get_open_positions()
        for pos in open_positions:
            try:
                closed = await self.position_service.close_position(pos['id'], reason="panic_sell")
                if closed:
                    messages.append(f"✅ Закрыта: {pos['pair']}")
                else:
                    messages.append(f"⚠️ Не удалось закрыть: {pos['pair']}")
            except Exception as e:
                messages.append(f"❌ Ошибка закрытия {pos['pair']}: {e}")

        # 4. Закрытие арбитражных позиций
        try:
            from .db.arbitrage_repo import ArbitrageRepository
            arb_repo = ArbitrageRepository()
            arb_positions = arb_repo.get_open_positions()
            for arb in arb_positions:
                try:
                    await arbitrage_service.close_arbitrage(arb['id'])
                    messages.append(f"✅ Арбитраж закрыт: {arb.get('pair', '?')}")
                except Exception as e:
                    messages.append(f"❌ Ошибка закрытия арбитража: {e}")
        except Exception as e:
            messages.append(f"⚠️ Арбитраж: {e}")

        # 5. Обновляем ордера в БД
        db_open_orders = self.position_service.trades_repo.get_open_orders()
        for order in db_open_orders:
            self.position_service.trades_repo.update_order_status(order['order_id'], 'Cancelled')

        messages.append("🏁 PANIC SELL завершён. Бот на паузе.")
        logger.warning("🏁 PANIC SELL завершён")
        return messages

    def _setup_scheduler(self):
        """Настроить задачи планировщика (декларативно — см. bot/services/job_scheduler.py)."""
        setup_jobs(self.scheduler, self)

    async def _update_daily_pnl_utc(self):
        try:
            from .db.daily_pnl_repo import DailyPnLRepository
            from .db.trades_repo import TradesRepository

            now_utc = datetime.now(timezone.utc)
            day_end = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            day_start = day_end - timedelta(days=1)
            date_utc = day_start.date().isoformat()

            closed_positions = TradesRepository.get_closed_positions_between_utc(day_start, day_end)

            gross_pnl = 0.0
            net_pnl = 0.0
            commission_paid = 0.0
            slippage = 0.0
            trades_count = 0
            wins = 0
            losses = 0

            for pos in closed_positions:
                gp = float(pos.get('gross_pnl') or 0)
                np = float(pos.get('net_pnl') or pos.get('realized_pnl') or 0)
                fee = float(pos.get('commission_paid') or 0)
                slip = float(pos.get('slippage') or 0)

                gross_pnl += gp
                net_pnl += np
                commission_paid += fee
                slippage += slip
                trades_count += 1
                if np > 0:
                    wins += 1
                elif np < 0:
                    losses += 1

            DailyPnLRepository.upsert_day(
                date_utc=date_utc,
                gross_pnl=gross_pnl,
                net_pnl=net_pnl,
                commission_paid=commission_paid,
                slippage=slippage,
                trades_count=trades_count,
                wins=wins,
                losses=losses,
            )

            logger.info(
                f"📅 daily_pnl UTC сохранён: {date_utc} | net={net_pnl:.2f} | trades={trades_count}"
            )
        except Exception as e:
            logger.error(f"❌ Ошибка daily_pnl UTC: {e}")
    
    # ========== ПЕРИОДИЧЕСКИЕ ЗАДАЧИ ==========
    
    async def _check_signals(self):
        """
        Поиск сигналов на вход.
        FUNNEL — воронка входа (_check_signals_funnel).
        LEGACY — pre-filter (RSI/EMA/MACD/объём 1h) → технические вето → вход по рынку.
        """
        if self.mode == "PAUSED":
            logger.debug("⏸️ Бот на паузе, пропускаем проверку сигналов")
            self._note_signal("⏸ пауза", "поиск входов не выполняется")
            return
        
        if self.mode == "RISK_ONLY":
            logger.debug("🟡 Режим RISK_ONLY (только сопровождение позиций), новые сделки запрещены")
            self._note_signal("🟡 только сопровождение", "новые входы запрещены")
            return
        
        if not self.auto_trading_enabled:
            logger.debug("🔕 Авто-торговля отключена")
            self._note_signal("🔕 авто-торговля выключена", "включается в Настройках")
            return

        # MARKET REGIME FILTER (4H, Variant B): полный запрет новых сделок в плохом режиме
        allowed, regime_reason = market_regime_service.is_trading_allowed()
        if not allowed:
            logger.info(f"🚦 {regime_reason}. Пропускаем поиск сигналов.")
            self._note_signal("🔴 светофор красный", regime_reason)
            return

        if ENTRY_STRATEGY == "FUNNEL":
            await self._check_signals_funnel()
            return
        
        logger.info("🔍 Этап 1: Pre-filter (технический анализ)...")
        
        active_pairs = self.get_active_pairs()
        
        # Фильтруем пары с открытыми позициями
        pairs_to_check = []
        for pair in active_pairs:
            can_open, _ = self.trading_service.can_open_position_for_pair(pair)
            if can_open:
                pairs_to_check.append(pair)
        
        if not pairs_to_check:
            logger.debug("ℹ️ Нет пар для проверки")
            return
        
        # Этап 1: Pre-filter (бесплатно, Python)
        candidates = prefilter_service.scan_and_filter(pairs_to_check, top_n=1)
        
        if not candidates:
            logger.info("ℹ️ Pre-filter: нет подходящих кандидатов")
            return
        
        # Этап 2: технические вето (RSI, рост за 24ч, Bollinger, стакан) и порог балла
        for candidate in candidates:
            pair = candidate['pair']
            try:
                analysis = await self.analysis_service.analyze_pair(pair, score=candidate['score'])
                
                if not analysis:
                    continue
                
                # Проверяем, стоит ли входить
                should_enter, reason = self.analysis_service.should_enter_trade(analysis)
                
                if should_enter:
                    logger.info(f"🎯 Сигнал на вход: {pair} | {reason}")
                    
                    # Проверяем риск-лимиты
                    can_trade, risk_reason = self.trading_service.check_risk_limits()
                    
                    if can_trade:
                        position, open_error = await self.run_exchange(self.trading_service.open_position, pair, analysis)
                        
                        if position:
                            logger.info(f"✅ Позиция открыта: {pair}")
                            msg = f"✅ Позиция открыта: {pair} @ ${position['entry_price']:.4f}"
                            await self._send_notification(msg)

                            try:
                                slip = float(position.get('slippage') or 0)
                                if abs(slip) > MAX_SLIPPAGE_PERCENT:
                                    await self._send_notification(
                                        f"💸 Проскальзывание {pair}: {slip*100:.2f}% > {MAX_SLIPPAGE_PERCENT*100:.2f}%"
                                    )

                                    exceed = self._record_slippage_event(pair, "OPEN", slip)
                                    if exceed >= 3:
                                        await self._ban_pair_for_slippage(pair, exceed)
                            except Exception:
                                pass
                            break
                        else:
                            # Логируем отказ с конкретной причиной из open_position
                            self._log_rejection(pair, analysis, open_error or "Ошибка открытия позиции")
                            logger.warning(f"⚠️ Не удалось открыть {pair}: {open_error}")
                    else:
                        # Логируем отказ: риск-лимиты
                        self._log_rejection(pair, analysis, f"Риск-лимит: {risk_reason}")
                        logger.warning(f"⚠️ Риск-лимит: {risk_reason}")
                else:
                    # Логируем отказ: слабый сигнал
                    self._log_rejection(pair, analysis, reason)
                    logger.debug(f"   {pair}: {reason}")
                
                # Задержка между запросами
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"❌ Ошибка проверки сигнала {pair}: {e}")
    
    def _note_signal(self, status: str, detail: str = "") -> None:
        """Итог последней проверки входов — для экрана «🧭 Сигналы»."""
        self.last_signal_check = {'at': time.time(), 'status': status, 'detail': detail}

    async def _check_signals_funnel(self):
        """
        Воронка входа (ENTRY_STRATEGY = "FUNNEL"):
        отбор пар → тренд 4h → откат 1h → триггер 15m → поток ордеров/OI/funding →
        балл >= FUNNEL_MIN_SCORE → корреляция → лимитный вход.
        За цикл открывается не больше одной позиции.
        """
        open_positions = self.trading_service.trades_repo.get_open_positions()
        open_pairs = [p['pair'] for p in open_positions]
        try:
            arb_pairs = [p['pair'] for p in arbitrage_service.repo.get_open_positions()]
        except Exception:
            arb_pairs = []

        can_trade, risk_reason = self.trading_service.check_risk_limits()
        if not can_trade:
            logger.info(f"🛑 Воронка: новые входы запрещены — {risk_reason}")
            self._note_signal("🛑 риск-лимит", risk_reason)
            return

        candidates = await asyncio.to_thread(
            entry_funnel_service.scan, exclude=open_pairs + arb_pairs, always_include=self.fixed_pairs
        )
        repo = entry_funnel_service.repo
        self._note_signal("🧭 воронка", f"кандидатов: {len(candidates)}" if candidates else "кандидатов нет")

        for c in candidates:
            pair, sid = c['pair'], c.get('signal_id')

            if self._is_pair_banned(pair):
                repo.update(sid, decision='SKIP', reason='пара забанена (проскальзывание)')
                continue

            blocked, why = await asyncio.to_thread(entry_funnel_service.correlation_block, pair, open_pairs)
            if blocked:
                logger.info(f"🔗 {pair}: пропуск — {why}")
                repo.update(sid, decision='SKIP', reason=why)
                continue

            position, err = await self.run_exchange(self.trading_service.open_position_with_plan, pair, c['plan'])
            if not position:
                repo.update(sid, decision='FAILED', reason=err or 'ошибка открытия')
                logger.warning(f"⚠️ {pair}: не удалось открыть — {err}")
                if err and 'лимит' in err:
                    break
                continue

            repo.update(sid, decision='OPENED', position_id=position['position_id'])
            self._note_signal("✅ открыта позиция", f"{pair}, балл {c['score']}")
            entry = position['entry_price']
            stop_pct = (entry - position['sl_price']) / entry * 100
            tp_pct = (position['tp_price'] - entry) / entry * 100
            await self._send_notification(
                f"✅ Позиция открыта: {pair} @ ${entry:.6g}\n"
                f"🧭 Балл воронки: {c['score']}/100 | триггер: {c['metrics'].get('trigger')}\n"
                f"🛑 SL ${position['sl_price']:.6g} (-{stop_pct:.2f}%) | 🎯 TP ${position['tp_price']:.6g} (+{tp_pct:.2f}%)"
            )

            slip = float(position.get('slippage') or 0)
            if abs(slip) > MAX_SLIPPAGE_PERCENT:
                await self._send_notification(
                    f"💸 Проскальзывание {pair}: {slip*100:.2f}% > {MAX_SLIPPAGE_PERCENT*100:.2f}%"
                )
                exceed = self._record_slippage_event(pair, "OPEN", slip)
                if exceed >= 3:
                    await self._ban_pair_for_slippage(pair, exceed)
            break

    def _log_rejection(self, pair: str, analysis: Dict, reason: str):
        """Записать причину отказа от входа в сделку для анализа"""
        try:
            score = analysis.get('score', 0)
            signal = analysis.get('signal', 'UNKNOWN')
            summary = analysis.get('summary', '')
            
            # Формируем детальную причину
            rejection_text = f"Score {score}. {reason}"
            if summary:
                rejection_text += f" | {summary[:100]}"
            
            # Получаем последний request_id для этой пары и обновляем
            # Или просто логируем в файл для анализа
            logger.info(f"📊 ОТКАЗ {pair}: {rejection_text[:150]}")
            
        except Exception as e:
            logger.debug(f"Ошибка логирования отказа: {e}")
    
    async def _update_balances(self):
        """Обновление балансов + контроль просадки от пика капитала"""
        try:
            deposit = self.balance_service.get_deposit_usdt()
            logger.debug(f"💰 Депозит USDT: {deposit:.2f}")
            from .services.account_guard import account_guard
            msg = account_guard.update_equity(getattr(self.balance_service, 'last_total_equity', None))
            if msg:
                await self._send_notification(msg)
        except Exception as e:
            logger.error(f"❌ Ошибка обновления балансов: {e}")
    
    async def _check_tpsl(self):
        """Проверка исполнения TP/SL"""
        # В реальности TP/SL будут исполняться биржей автоматически
        # Эта функция для дополнительной проверки и синхронизации
        try:
            await self.position_service.update_positions_prices()
        except Exception as e:
            logger.error(f"❌ Ошибка проверки TP/SL: {e}")
    
    async def _update_positions_prices(self):
        """Обновление цен открытых позиций"""
        try:
            await self.position_service.update_positions_prices()
        except Exception as e:
            logger.error(f"❌ Ошибка обновления цен: {e}")

    async def _sync_orders_and_trades(self):
        try:
            messages = await self.position_service.sync_orders_and_trades() or []
            for msg in messages:
                await self._send_notification(msg)
        except Exception as e:
            logger.error(f"❌ Ошибка синхронизации ордеров: {e}")

    async def _reconcile_orphan_orders(self):
        try:
            messages = await self.position_service.reconcile_orphan_orders()
            for msg in messages:
                await self._send_notification(msg)
        except Exception as e:
            logger.error(f"❌ Ошибка reconciliation: {e}")
            
    async def _update_trailing_stops(self):
        """Обновление трейлинг-стопов"""
        try:
            messages = await self.position_service.update_trailing_stops()
            for msg in messages:
                await self._send_notification(msg)
        except Exception as e:
            logger.error(f"❌ Ошибка Trailing Stop: {e}")
    
    async def _check_breakeven(self):
        """Проверка и активация безубытка"""
        try:
            messages = await self.position_service.check_breakeven()
            for msg in messages:
                await self._send_notification(msg)
        except Exception as e:
            logger.error(f"❌ Ошибка Breakeven: {e}")
    
    async def _check_time_exit(self):
        """Проверка и закрытие мёртвых позиций"""
        try:
            messages = await self.position_service.check_time_exit()
            for msg in messages:
                await self._send_notification(msg)
        except Exception as e:
            logger.error(f"❌ Ошибка Time Exit: {e}")
    
    async def _auto_create_missing_sl(self):
        """Автоматическое создание SL для позиций без защиты"""
        try:
            messages = await self.position_service.auto_create_missing_sl()
            for msg in messages:
                await self._send_notification(msg)
        except Exception as e:
            logger.error(f"❌ Ошибка Auto SL: {e}")

    async def _emergency_sl_watchdog(self):
        try:
            messages = await self.position_service.emergency_sl_watchdog()
            for msg in messages:
                await self._send_notification(msg)
        except Exception as e:
            logger.error(f"❌ Ошибка Emergency SL: {e}")
    
    async def _check_dca(self):
        """Проверка необходимости Smart DCA"""
        if self.mode == "PAUSED":
            return
        
        try:
            from .db.trades_repo import TradesRepository
            trades_repo = TradesRepository()
            open_positions = trades_repo.get_open_positions()
            
            for pos in open_positions:
                action = self.trading_service.get_add_action(pos)
                if action:
                    pair = pos['pair']
                    logger.info(f"📉 {action} триггер для {pair}, запрашиваем анализ...")
                    
                    # Запрашиваем свежий анализ
                    analysis = await self.analysis_service.analyze_pair(pair)
                    
                    if analysis:
                        result = await self.trading_service.add_to_position(pos['id'], analysis)
                        
                        if result:
                            label = result.get('action') or action
                            msg = f"➕ {label} {pair}: докупка | Новая средняя: ${result['new_avg']:.4f}"
                            await self._send_notification(msg)

                            try:
                                slip = float(result.get('slippage') or 0)
                                if abs(slip) > MAX_SLIPPAGE_PERCENT:
                                    await self._send_notification(
                                        f"💸 Проскальзывание {pair} ({label}): {slip*100:.2f}% > {MAX_SLIPPAGE_PERCENT*100:.2f}%"
                                    )

                                    exceed = self._record_slippage_event(pair, str(label), slip)
                                    if exceed >= 3:
                                        await self._ban_pair_for_slippage(pair, exceed)
                            except Exception:
                                pass
                            
        except Exception as e:
            logger.error(f"❌ Ошибка Smart DCA: {e}")
    
    async def _reset_daily_limits(self):
        """Сброс дневных лимитов в полночь"""
        logger.info("🔄 Сброс дневных лимитов...")
        
        # Раньше здесь в полночь снималась ЛЮБАЯ пауза — в том числе после PANIC SELL.
        # Теперь автоматически снимается только пауза из-за дневного лимита.
        if self.mode == "PAUSED" and self.pause_reason == "daily_limit":
            self.mode = "ACTIVE"
            self.pause_reason = ""
            logger.info("▶️ Бот возобновлён после сброса дневных лимитов")
        elif self.mode == "PAUSED":
            logger.info(f"⏸ Пауза ({self.pause_reason or 'вручную'}) сохраняется — снимается кнопкой в Telegram")
    
    async def _update_market_pairs(self):
        """Обновление списка пар через сканер (с сохранением на 24ч)"""
        if not self.scanner_enabled:
            return
            
        logger.info("🔍 Запуск сканера рынка...")
        try:
            # 1. Получаем новые топ монеты (уже подходят под фильтры)
            new_top = self.scanner_service.get_top_gainers(limit=5)
            
            # 2. Обновляем время существующих или добавляем новые
            now = time.time()
            for p in new_top:
                self.dynamic_pairs_data[p] = now
            
            # 3. Удаляем те, что старше 24ч и НЕ в новом ТОПе
            pairs_to_remove = []
            for p, added_at in self.dynamic_pairs_data.items():
                if now - added_at > 24 * 3600 and p not in new_top:
                    pairs_to_remove.append(p)
            
            for p in pairs_to_remove:
                del self.dynamic_pairs_data[p]
                logger.debug(f"🗑️ Динамическая пара {p} удалена по времени (24ч)")
            
            # 4. Ограничиваем общий список динамических пар (макс 10)
            # Сортируем по времени добавления/обновления
            sorted_pairs = sorted(self.dynamic_pairs_data.items(), key=lambda x: x[1], reverse=True)
            self.dynamic_pairs = [p for p, t in sorted_pairs[:10]]
            
            if self.dynamic_pairs:
                logger.info(f"✅ Список динамических пар обновлён: {', '.join(self.dynamic_pairs)}")
                # Обновляем подписки WS
                websocket_service.subscribe(self.get_active_pairs())
            else:
                logger.info("ℹ️ Динамических пар пока нет")
                
        except Exception as e:
            logger.error(f"❌ Ошибка обновления пар: {e}")
            
    # ========== НАСТРОЙКИ ==========
            
    def _load_settings(self):
        """Загрузить настройки из БД"""
        try:
            with db.get_connection() as conn:
                cursor = conn.cursor()
                
                # 1. Scanner
                cursor.execute("SELECT value FROM settings WHERE key = 'scanner_enabled'")
                row = cursor.fetchone()
                if row:
                    self.scanner_enabled = row[0] == '1'
                    logger.info(f"⚙️ Загружена настройка сканера: {'ВКЛ' if self.scanner_enabled else 'ВЫКЛ'}")
                
                # 2. Auto Trading
                cursor.execute("SELECT value FROM settings WHERE key = 'auto_trading_enabled'")
                row = cursor.fetchone()
                if row:
                    self.auto_trading_enabled = row[0] == '1'
                    logger.info(f"⚙️ Загружена настройка авто-торговли: {'ВКЛ' if self.auto_trading_enabled else 'ВЫКЛ'}")
                else:
                    # По умолчанию уже True в __init__, можно явно сохранить или оставить как есть
                    pass

                # 3. Notifications
                cursor.execute("SELECT value FROM settings WHERE key = 'notifications_enabled'")
                row = cursor.fetchone()
                if row:
                    self.notifications_enabled = row[0] == '1'
                    logger.info(
                        f"⚙️ Загружена настройка уведомлений: {'ВКЛ' if self.notifications_enabled else 'ВЫКЛ'}"
                    )

                # 4. Alerts
                cursor.execute("SELECT value FROM settings WHERE key = 'alerts_enabled'")
                row = cursor.fetchone()
                if row:
                    self.alerts_enabled = row[0] == '1'
                    logger.info(
                        f"⚙️ Загружена настройка алертов: {'ВКЛ' if self.alerts_enabled else 'ВЫКЛ'}"
                    )

        except Exception as e:
            logger.error(f"❌ Ошибка загрузки настроек: {e}")

    def get_active_pairs(self) -> list[str]:
        """Получить общий список активных пар"""
        # Объединяем и убираем дубликаты (сохраняя порядок)
        all_pairs = self.fixed_pairs + self.dynamic_pairs
        unique_pairs = list(dict.fromkeys(all_pairs))
        return [p for p in unique_pairs if not self._is_pair_banned(p)]

    def is_scanner_enabled(self) -> bool:
        """Проверить статус сканера"""
        return self.scanner_enabled

    def toggle_scanner(self) -> bool:
        """Переключить сканер"""
        self.scanner_enabled = not self.scanner_enabled
        
        # Сохраняем в БД
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    ('scanner_enabled', '1' if self.scanner_enabled else '0')
                )
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения настроек: {e}")
            
        status = "включён" if self.scanner_enabled else "выключен"
        logger.info(f"🔄 Сканер {status}")
        
        # Если включили, можно сразу запустить обновление (в фоне)
        if self.scanner_enabled:
            asyncio.create_task(self.run_job('_update_market_pairs'))
        else:
            self.dynamic_pairs = []
            self.dynamic_pairs_data = {}
        
        return self.scanner_enabled
    
    def get_mode(self) -> str:
        """Получить текущий режим бота"""
        return self.mode
    
    def set_mode(self, mode: str, reason: str = "manual"):
        """Установить режим бота"""
        if mode not in ["ACTIVE", "RISK_ONLY", "PAUSED"]:
            logger.error(f"❌ Неверный режим: {mode}")
            return
        
        old_mode = self.mode
        self.mode = mode
        self.pause_reason = reason if mode == "PAUSED" else ""
        
        logger.info(f"🔄 Режим изменён: {old_mode} → {mode}")
    
    def is_auto_trading_enabled(self) -> bool:
        """Проверить, включена ли авто-торговля"""
        return self.auto_trading_enabled
    
    def toggle_auto_trading(self) -> bool:
        """Переключить авто-торговлю"""
        self.auto_trading_enabled = not self.auto_trading_enabled
        
        # Сохраняем в БД
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    ('auto_trading_enabled', '1' if self.auto_trading_enabled else '0')
                )
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения настроек: {e}")
            
        status = "включена" if self.auto_trading_enabled else "выключена"
        logger.info(f"🔄 Авто-торговля {status}")
        
        return self.auto_trading_enabled

    def is_notifications_enabled(self) -> bool:
        """Проверить, включены ли уведомления в Telegram"""
        return self.notifications_enabled

    def toggle_notifications(self) -> bool:
        """Переключить уведомления в Telegram"""
        self.notifications_enabled = not self.notifications_enabled

        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    ('notifications_enabled', '1' if self.notifications_enabled else '0')
                )
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения settings.notifications_enabled: {e}")

        status = "включены" if self.notifications_enabled else "выключены"
        logger.info(f"🔔 Уведомления {status}")
        return self.notifications_enabled

    def is_alerts_enabled(self) -> bool:
        """Проверить, включены ли алерты в Telegram"""
        return self.alerts_enabled

    def toggle_alerts(self) -> bool:
        """Переключить алерты в Telegram"""
        self.alerts_enabled = not self.alerts_enabled

        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    ('alerts_enabled', '1' if self.alerts_enabled else '0')
                )
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения settings.alerts_enabled: {e}")

        status = "включены" if self.alerts_enabled else "выключены"
        logger.info(f"🚨 Алерты {status}")
        return self.alerts_enabled
    
    # ========== АВТО-АРБИТРАЖ ==========
    
    async def _auto_arbitrage(self):
        """Автоматический поиск и открытие арбитражных позиций"""
        if self.mode == "PAUSED":
            return
        
        try:
            # В красном режиме (Светофор) усиливаем арбитраж, чтобы капитал не простаивал
            max_positions = ARBITRAGE_MAX_POSITIONS
            position_size_usd = ARBITRAGE_POSITION_SIZE_USD
            try:
                allowed, _ = market_regime_service.is_trading_allowed()
                is_red = not bool(allowed)
            except Exception:
                is_red = False

            if ARBITRAGE_RED_MODE_ENABLED and is_red:
                max_positions = int(ARBITRAGE_RED_MAX_POSITIONS)
                # percent-based sizing: allocate up to X% of stable deposit (USDT+USDC)
                try:
                    balances = balance_service.get_wallet_balance() or {}
                    usdt_total = float((balances.get("USDT") or {}).get("total") or 0)
                    usdc_total = float((balances.get("USDC") or {}).get("total") or 0)
                    stable_total = usdt_total + usdc_total
                except Exception:
                    stable_total = 0.0

                # Current arbitrage notional (approx): sum(entry_price * qty) for OPEN positions
                try:
                    dash = arbitrage_service.get_dashboard()
                    current_arb_value = float(dash.get("total_value") or 0)
                except Exception:
                    current_arb_value = 0.0

                total_allocation = max(0.0, stable_total * float(ARBITRAGE_RED_TOTAL_ALLOCATION_PCT))
                remaining_allocation = max(0.0, total_allocation - current_arb_value)

                remaining_slots = max(0, max_positions - len(arbitrage_service.repo.get_open_positions()))
                if remaining_slots <= 0:
                    logger.debug(
                        f"📊 Арбитраж (красный режим): лимит позиций достигнут ({max_positions})"
                    )
                    return

                # Split remaining allocation across remaining slots
                per_position = remaining_allocation / max(1, remaining_slots)
                min_size = float(ARBITRAGE_RED_MIN_POSITION_SIZE_USD)
                position_size_usd = max(min_size, per_position)

                logger.info(
                    "💹 Авто-арбитраж (красный режим): "
                    f"лимит={max_positions}, аллокация={float(ARBITRAGE_RED_TOTAL_ALLOCATION_PCT)*100:.0f}%, "
                    f"стейблы≈${stable_total:.2f}, в_арбитраже≈${current_arb_value:.2f}, "
                    f"остаток≈${remaining_allocation:.2f}, размер≈${position_size_usd:.2f}"
                )

            # Проверяем количество открытых арбитражных позиций
            open_positions = arbitrage_service.repo.get_open_positions()
            if len(open_positions) >= max_positions:
                logger.debug(f"📊 Арбитраж: достигнут лимит позиций ({len(open_positions)}/{max_positions})")
                return
            
            # Сканируем только whitelist (BTC/ETH): остальные пары всё равно не допускаются
            opportunities = arbitrage_service.scan_funding_rates(
                pairs=sorted(arbitrage_service.SAFE_COLLATERAL_COINS)
            )
            
            # Вход только если funding окупает комиссии полного круга достаточно быстро
            good_opps = [o for o in opportunities if o.get('entry_ok')]
            
            if not good_opps:
                best = ", ".join(f"{o['pair']} {o['funding_pct']:.4f}% ({o['reason']})" for o in opportunities)
                logger.info(f"📊 Арбитраж: входа нет — {best or 'нет данных'}")
                return
            
            # Пары с открытым арбитражем или обычной позицией бота пропускаем
            open_pairs = {p['pair'] for p in open_positions}
            open_pairs |= {p['pair'] for p in self.trading_service.trades_repo.get_open_positions()}
            
            for opp in good_opps:
                if opp['pair'] in open_pairs:
                    continue  # Уже есть позиция
                
                if len(open_positions) >= max_positions:
                    break
                
                # Открываем арбитраж
                success, message = await arbitrage_service.open_arbitrage(
                    opp['pair'], 
                    position_size_usd
                )
                
                if success:
                    logger.info(f"✅ АВТО-АРБИТРАЖ: {message}")
                    await self._send_notification(f"💹 АВТО-АРБИТРАЖ: {message}")
                    open_positions = arbitrage_service.repo.get_open_positions()
                else:
                    logger.warning(f"⚠️ Арбитраж {opp['pair']}: {message}")
                
        except Exception as e:
            logger.error(f"❌ Ошибка авто-арбитража: {e}")
    
    async def _update_arbitrage_funding(self):
        """Учесть начисленный funding и закрыть позиции с устойчиво отрицательной ставкой"""
        try:
            messages = await arbitrage_service.settle_funding()
            for msg in messages:
                await self._send_notification(msg)
            logger.info("💰 Funding арбитражных позиций обновлён")
        except Exception as e:
            logger.error(f"❌ Ошибка обновления funding: {e}")


# Создание глобального экземпляра
controller = BotController()

