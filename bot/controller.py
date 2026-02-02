"""
Главный контроллер бота - управление циклами и планировщиком
"""
import asyncio
import time
from typing import Dict, List
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from config.settings import logger, TIMEZONE
from config.trading_config import (
    SIGNAL_CHECK_INTERVAL,
    BALANCE_UPDATE_INTERVAL,
    TPSL_CHECK_INTERVAL,
    DEFAULT_TRADING_PAIRS,
    MAX_CONSECUTIVE_LOSSES,
    ARBITRAGE_ENABLED,
    ARBITRAGE_CHECK_INTERVAL,
    ARBITRAGE_MIN_FUNDING_RATE,
    ARBITRAGE_MAX_POSITIONS,
    ARBITRAGE_POSITION_SIZE_USD
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
from .perplexity_client import perplexity_client


class BotController:
    """Главный контроллер и планировщик задач"""
    
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        self.mode = "ACTIVE"  # ACTIVE, RISK_ONLY, PAUSED
        self.mode = "ACTIVE"  # ACTIVE, RISK_ONLY, PAUSED
        self.auto_trading_enabled = True
        self.scanner_enabled = False  # По умолчанию выключен
        
        # Пары
        self.trading_service = trading_service
        self.position_service = position_service
        self.analysis_service = analysis_service
        self.balance_service = balance_service
        self.scanner_service = scanner_service
        
        self.fixed_pairs = list(DEFAULT_TRADING_PAIRS)
        self.dynamic_pairs = []
        self.dynamic_pairs_data = {}  # {symbol: timestamp} для 24ч памяти

        
        self.is_running = False
        self.notification_callback = None
    
    def set_notifier(self, callback):
        """Установить callback для уведомлений"""
        self.notification_callback = callback

    async def _send_notification(self, message: str):
        """Отправить уведомление если callback установлен"""
        if self.notification_callback:
            try:
                await self.notification_callback(message)
            except Exception as e:
                logger.error(f"❌ Ошибка отправки уведомления: {e}")
    
    async def start(self):
        """Запустить контроллер и планировщик"""
        logger.info("🚀 Запуск контроллера...")
        
        # Проверка доступности Perplexity
        if not perplexity_client.is_available:
            logger.warning("⚠️ Perplexity недоступен, переход в режим RISK_ONLY")
            self.mode = "RISK_ONLY"
        
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
            asyncio.create_task(self._update_market_pairs())
            
        logger.info("✅ Контроллер запущен")
    
    async def stop(self):
        """Остановить контроллер"""
        logger.info("🛑 Остановка контроллера...")
        
        self.is_running = False
        
        # Проверяем, что scheduler запущен перед остановкой
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
        
        # Закрываем подключение к БД
        db.close()
        
        logger.info("✅ Контроллер остановлен")
    
    def _setup_scheduler(self):
        """Настроить задачи планировщика"""
        
        # 1. Проверка сигналов (каждые N секунд)
        self.scheduler.add_job(
            self._check_signals,
            trigger=IntervalTrigger(seconds=SIGNAL_CHECK_INTERVAL),
            id='check_signals',
            name='Проверка торговых сигналов',
            max_instances=1
        )
        
        # 2. Обновление балансов (каждую минуту)
        self.scheduler.add_job(
            self._update_balances,
            trigger=IntervalTrigger(seconds=BALANCE_UPDATE_INTERVAL),
            id='update_balances',
            name='Обновление балансов',
            max_instances=1
        )
        
        # 3. Проверка TP/SL (каждые 10 секунд)
        self.scheduler.add_job(
            self._check_tpsl,
            trigger=IntervalTrigger(seconds=TPSL_CHECK_INTERVAL),
            id='check_tpsl',
            name='Проверка TP/SL',
            max_instances=1
        )
        
        # 4. Обновление цен позиций (каждые 30 секунд)
        self.scheduler.add_job(
            self._update_positions_prices,
            trigger=IntervalTrigger(seconds=30),
            id='update_prices',
            name='Обновление цен позиций',
            max_instances=1
        )

        self.scheduler.add_job(
            self._sync_orders_and_trades,
            trigger=IntervalTrigger(seconds=60),
            id='sync_orders',
            name='Sync Orders/Trades',
            max_instances=1
        )
        
        # 5. Trailing Stop (каждые 30 секунд)
        self.scheduler.add_job(
            self._update_trailing_stops,
            trigger=IntervalTrigger(seconds=30),
            id='trailing_stop',
            name='Trailing Stop',
            max_instances=1
        )
        
        # 6. Breakeven (каждые 30 секунд)
        self.scheduler.add_job(
            self._check_breakeven,
            trigger=IntervalTrigger(seconds=30),
            id='check_breakeven',
            name='Breakeven Check',
            max_instances=1
        )
        
        # 7. Time Exit (каждые 5 минут)
        self.scheduler.add_job(
            self._check_time_exit,
            trigger=IntervalTrigger(minutes=5),
            id='check_time_exit',
            name='Time Exit Check',
            max_instances=1
        )
        
        # 8. Smart DCA (каждые 2 минуты)
        self.scheduler.add_job(
            self._check_dca,
            trigger=IntervalTrigger(minutes=2),
            id='check_dca',
            name='Smart DCA Check',
            max_instances=1
        )
        
        # 9. Сброс дневных лимитов (в полночь по Europe/Kaliningrad)
        self.scheduler.add_job(
            self._reset_daily_limits,
            trigger=CronTrigger(hour=0, minute=0, timezone=TIMEZONE),
            id='reset_daily',
            name='Сброс дневных лимитов',
            max_instances=1
        )
        
        # 6. Обновление динамических пар (каждый час)
        self.scheduler.add_job(
            self._update_market_pairs,
            trigger=IntervalTrigger(hours=1),
            id='update_pairs',
            name='Обновление списка пар',
            max_instances=1
        )
        
        # 10. Авто-создание SL для позиций без защиты (каждые 2 минуты)
        self.scheduler.add_job(
            self._auto_create_missing_sl,
            trigger=IntervalTrigger(minutes=2),
            id='auto_sl',
            name='Auto SL Creation',
            max_instances=1
        )

        self.scheduler.add_job(
            self._emergency_sl_watchdog,
            trigger=IntervalTrigger(seconds=15),
            id='emergency_sl',
            name='Emergency SL Watchdog',
            max_instances=1
        )
        
        # 11. Авто-арбитраж: сканирование и открытие (каждый час)
        if ARBITRAGE_ENABLED:
            self.scheduler.add_job(
                self._auto_arbitrage,
                trigger=IntervalTrigger(seconds=ARBITRAGE_CHECK_INTERVAL),
                id='auto_arbitrage',
                name='Auto Arbitrage',
                max_instances=1
            )
            
            # 12. Обновление funding каждые 8 часов (0:00, 8:00, 16:00 UTC)
            self.scheduler.add_job(
                self._update_arbitrage_funding,
                trigger=CronTrigger(hour='0,8,16', minute=5, timezone='UTC'),
                id='update_arbitrage_funding',
                name='Update Arbitrage Funding',
                max_instances=1
            )
        
        logger.info("📅 Планировщик настроен")
    
    # ========== ПЕРИОДИЧЕСКИЕ ЗАДАЧИ ==========
    
    async def _check_signals(self):
        """
        Двухэтапная проверка сигналов (стратегия "Воронка"):
        Этап 1: Python pre-filter (технический анализ)
        Этап 2: AI анализ только для топ-2 кандидатов
        """
        if self.mode == "PAUSED":
            logger.debug("⏸️ Бот на паузе, пропускаем проверку сигналов")
            return
        
        if self.mode == "RISK_ONLY":
            logger.debug("🟡 Режим RISK_ONLY, новые сделки запрещены")
            return
        
        if not self.auto_trading_enabled:
            logger.debug("🔕 Авто-торговля отключена")
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
            logger.info("ℹ️ Pre-filter: нет подходящих кандидатов для AI")
            return
        
        # Этап 2: AI анализ (платно, Perplexity) - только для топ-2
        logger.info(f"🤖 Этап 2: AI анализ {len(candidates)} кандидатов...")
        
        for candidate in candidates:
            pair = candidate['pair']
            try:
                # AI анализ (использует Perplexity)
                analysis = await self.analysis_service.analyze_pair(pair, use_cache=True)
                
                if not analysis:
                    continue
                
                # Проверяем, стоит ли входить
                should_enter, reason = self.analysis_service.should_enter_trade(analysis)
                
                if should_enter:
                    logger.info(f"🎯 Сигнал на вход: {pair} | {reason}")
                    
                    # Проверяем риск-лимиты
                    can_trade, risk_reason = self.trading_service.check_risk_limits()
                    
                    if can_trade:
                        position, open_error = await self.trading_service.open_position(pair, analysis)
                        
                        if position:
                            logger.info(f"✅ Позиция открыта: {pair}")
                            msg = f"✅ Позиция открыта: {pair} @ ${position['entry_price']:.4f}"
                            await self._send_notification(msg)
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
    
    def _log_rejection(self, pair: str, analysis: Dict, reason: str):
        """Записать причину отказа от входа в сделку для анализа"""
        try:
            from .db.llm_requests_repo import LLMRequestsRepository
            
            score = analysis.get('score', 0)
            signal = analysis.get('signal', 'UNKNOWN')
            summary = analysis.get('summary', '')
            
            # Формируем детальную причину
            rejection_text = f"Score {score}. {reason}"
            if summary:
                rejection_text += f" | AI резюме: {summary[:100]}"
            
            # Получаем последний request_id для этой пары и обновляем
            # Или просто логируем в файл для анализа
            logger.info(f"📊 ОТКАЗ {pair}: {rejection_text[:150]}")
            
        except Exception as e:
            logger.debug(f"Ошибка логирования отказа: {e}")
    
    async def _update_balances(self):
        """Обновление балансов"""
        try:
            deposit = self.balance_service.get_deposit_usdt()
            logger.debug(f"💰 Депозит USDT: {deposit:.2f}")
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
            await self.position_service.sync_orders_and_trades()
        except Exception as e:
            logger.error(f"❌ Ошибка синхронизации ордеров: {e}")
            
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
                # Проверяем, нужна ли докупка
                if self.trading_service.check_dca_trigger(pos):
                    pair = pos['pair']
                    logger.info(f"📉 DCA триггер для {pair}, запрашиваем анализ...")
                    
                    # Запрашиваем свежий анализ
                    analysis = await self.analysis_service.analyze_pair(pair, use_cache=False)
                    
                    if analysis:
                        result = await self.trading_service.add_to_position(pos['id'], analysis)
                        
                        if result:
                            msg = f"➕ DCA {pair}: докупка #{result['dca_count']} | Новая средняя: ${result['new_avg']:.4f}"
                            await self._send_notification(msg)
                            
        except Exception as e:
            logger.error(f"❌ Ошибка Smart DCA: {e}")
    
    async def _reset_daily_limits(self):
        """Сброс дневных лимитов в полночь"""
        logger.info("🔄 Сброс дневных лимитов...")
        
        # Если бот был на паузе из-за дневных лимитов, возобновляем
        if self.mode == "PAUSED":
            self.mode = "ACTIVE"
            logger.info("▶️ Бот возобновлён после сброса дневных лимитов")
        
        # Проверяем доступность Perplexity
        if self.mode == "RISK_ONLY":
            if perplexity_client.is_available:
                can_use, reason = perplexity_client.check_limits()
                if can_use:
                    self.mode = "ACTIVE"
                    logger.info("✅ Perplexity доступен, переход в режим ACTIVE")
                    self.mode = "ACTIVE"
                    logger.info("✅ Perplexity доступен, переход в режим ACTIVE")
    
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

        except Exception as e:
            logger.error(f"❌ Ошибка загрузки настроек: {e}")

    def get_active_pairs(self) -> list[str]:
        """Получить общий список активных пар"""
        # Объединяем и убираем дубликаты (сохраняя порядок)
        all_pairs = self.fixed_pairs + self.dynamic_pairs
        return list(dict.fromkeys(all_pairs))

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
            asyncio.create_task(self._update_market_pairs())
        else:
            self.dynamic_pairs = []
            self.dynamic_pairs_data = {}
        
        return self.scanner_enabled
    
    def get_mode(self) -> str:
        """Получить текущий режим бота"""
        return self.mode
    
    def set_mode(self, mode: str):
        """Установить режим бота"""
        if mode not in ["ACTIVE", "RISK_ONLY", "PAUSED"]:
            logger.error(f"❌ Неверный режим: {mode}")
            return
        
        old_mode = self.mode
        self.mode = mode
        
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
    
    # ========== АВТО-АРБИТРАЖ ==========
    
    async def _auto_arbitrage(self):
        """Автоматический поиск и открытие арбитражных позиций"""
        if self.mode == "PAUSED":
            return
        
        try:
            # Проверяем количество открытых арбитражных позиций
            open_positions = arbitrage_service.repo.get_open_positions()
            if len(open_positions) >= ARBITRAGE_MAX_POSITIONS:
                logger.debug(f"📊 Арбитраж: достигнут лимит позиций ({len(open_positions)}/{ARBITRAGE_MAX_POSITIONS})")
                return
            
            # Сканируем возможности
            opportunities = arbitrage_service.scan_funding_rates()
            
            # Фильтруем по минимальному funding rate
            good_opps = [o for o in opportunities if o['funding_rate'] >= ARBITRAGE_MIN_FUNDING_RATE]
            
            if not good_opps:
                logger.debug("📊 Арбитраж: нет выгодных возможностей")
                return
            
            # Проверяем, какие пары уже открыты
            open_pairs = {p['pair'] for p in open_positions}
            
            for opp in good_opps:
                if opp['pair'] in open_pairs:
                    continue  # Уже есть позиция
                
                if len(open_positions) >= ARBITRAGE_MAX_POSITIONS:
                    break
                
                # Открываем арбитраж
                success, message = await arbitrage_service.open_arbitrage(
                    opp['pair'], 
                    ARBITRAGE_POSITION_SIZE_USD
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
        """Обновить накопленный funding для всех арбитражных позиций"""
        try:
            arbitrage_service.update_funding_for_all()
            logger.info("💰 Funding арбитражных позиций обновлён")
        except Exception as e:
            logger.error(f"❌ Ошибка обновления funding: {e}")


# Создание глобального экземпляра
controller = BotController()

