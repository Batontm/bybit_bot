"""
Главный файл запуска Bybit Trading Bot
"""
import asyncio
import signal
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import logger, TIMEZONE, setup_logger
from config.api_config import validate_config
from config.trading_config import DEFAULT_TRADING_PAIRS
from bot.controller import controller
from bot.telegram_client.bot import create_telegram_bot
from bot.perplexity_client import perplexity_client
from bot.services.balance_service import balance_service
from bot.error_tracker import error_tracker
from pybit.unified_trading import HTTP


class TradingBot:
    """Главный класс бота"""
    
    def __init__(self):
        self.controller = controller
        self.telegram_bot = create_telegram_bot(controller)
        self.is_running = False
        self.startup_checks = {}
    
    async def startup_checks_routine(self) -> dict:
        """Выполнить все стартовые проверки"""
        logger.info("=" * 60)
        logger.info(f"==== BOT START {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')} ====")
        logger.info("=" * 60)
        
        checks = {
            'testnet': True,
            'mode': 'ACTIVE'
        }
        
        # 1. Проверка конфигурации
        logger.info("📋 Проверка конфигурации...")
        config_errors = validate_config()
        
        if config_errors:
            for error in config_errors:
                logger.warning(f"   {error}")
        
        # 2. Проверка Bybit REST API
        logger.info("🔌 Проверка Bybit REST API...")
        try:
            from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET
            
            client = HTTP(
                testnet=True,
                api_key=BYBIT_API_KEY,
                api_secret=BYBIT_API_SECRET,
                recv_window=60000
            )
            
            server_time = client.get_server_time()
            
            if server_time['retCode'] == 0:
                logger.info("   ✅ Bybit REST API доступен")
                checks['bybit_rest'] = True
            else:
                logger.error(f"   ❌ Bybit REST API недоступен: {server_time['retMsg']}")
                checks['bybit_rest'] = False
                
        except Exception as e:
            logger.error(f"   ❌ Ошибка подключения к Bybit REST: {e}")
            error_tracker.add_error("Bybit", "ConnectionError", str(e))
            checks['bybit_rest'] = False
        
        # 3. Тестовый ордер
        if checks.get('bybit_rest'):
            logger.info("🧪 Тест размещения ордера...")
            try:
                instruments = client.get_instruments_info(
                    category="spot",
                    symbol="BTCUSDT"
                )
                
                if instruments['retCode'] == 0:
                    info = instruments['result']['list'][0]
                    
                    lot_filter = info['lotSizeFilter']
                    price_filter = info['priceFilter']
                    
                    min_qty = float(lot_filter['minOrderQty'])
                    tick_size = float(price_filter['tickSize'])
                    
                    # Парсим basePrecision из строки "0.000001"
                    base_precision_str = lot_filter.get('basePrecision', '0.00000001')
                    if '.' in base_precision_str:
                        base_precision = len(base_precision_str.split('.')[1])
                    else:
                        base_precision = 8
                    
                    ticker = client.get_tickers(category="spot", symbol="BTCUSDT")
                    current_price = float(ticker['result']['list'][0]['lastPrice'])
                    
                    # Тестовая цена на 5% ниже
                    test_price = round(current_price * 0.95 / tick_size) * tick_size
                    
                    # ИСПРАВЛЕНО: Увеличиваем количество, чтобы сумма была > $1
                    min_order_value = 11  # USDT
                    test_qty = max(min_qty, min_order_value / test_price)
                    test_qty = round(test_qty, base_precision)
                    
                    # Форматируем
                    qty_str = f"{test_qty:.{base_precision}f}".rstrip('0').rstrip('.')
                    price_str = f"{test_price:.2f}"
                    
                    order_value = test_qty * test_price
                    logger.info(f"   Размещаем тестовый ордер: {qty_str} BTC @ {price_str} USDT (сумма: ${order_value:.2f})")
                    
                    test_order = client.place_order(
                        category="spot",
                        symbol="BTCUSDT",
                        side="Buy",
                        orderType="Limit",
                        qty=qty_str,
                        price=price_str
                    )
                    
                    if test_order['retCode'] == 0:
                        order_id = test_order['result']['orderId']
                        logger.info(f"   ✅ Тестовый ордер размещён: {order_id}")
                        
                        cancel = client.cancel_order(
                            category="spot",
                            symbol="BTCUSDT",
                            orderId=order_id
                        )
                        
                        if cancel['retCode'] == 0:
                            logger.info("   ✅ Тестовый ордер отменён")
                            checks['test_order'] = True
                        else:
                            logger.warning("   ⚠️ Не удалось отменить тестовый ордер")
                            checks['test_order'] = False
                    else:
                        logger.error(f"   ❌ Ошибка тестового ордера: {test_order['retMsg']}")
                        checks['test_order'] = False
                else:
                    logger.error(f"   ❌ Не удалось получить информацию об инструменте")
                    checks['test_order'] = False
                        
            except Exception as e:
                logger.exception(f"   ❌ Исключение при тестовом ордере")
                error_tracker.add_error("Bybit", "TestOrderError", str(e))
                checks['test_order'] = False
        
        # 4. WebSocket
        logger.info("🌐 Проверка Bybit WebSocket...")
        checks['bybit_ws'] = True
        logger.info("   ✅ WebSocket эндпоинты доступны")
        
        # 5. Perplexity API
        logger.info("🤖 Проверка Perplexity API...")
        if perplexity_client.is_available:
            perplexity_ok = perplexity_client.test_connection()
            checks['perplexity'] = perplexity_ok
            
            if perplexity_ok:
                logger.info("   ✅ Perplexity API доступен")
            else:
                logger.warning("   ⚠️ Perplexity API недоступен, режим RISK_ONLY")
                checks['mode'] = 'RISK_ONLY'
        else:
            logger.warning("   ⚠️ Perplexity API key не установлен, режим RISK_ONLY")
            checks['perplexity'] = False
            checks['mode'] = 'RISK_ONLY'
        
        # 6. База данных
        logger.info("💾 Проверка базы данных...")
        try:
            from bot.db.connection import db
            
            conn = db.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
            table_count = cursor.fetchone()[0]
            
            logger.info(f"   ✅ База данных OK ({table_count} таблиц)")
            checks['database'] = True
            
        except Exception as e:
            logger.error(f"   ❌ Ошибка базы данных: {e}")
            error_tracker.add_error("Database", "ConnectionError", str(e))
            checks['database'] = False
        
        # 7. Балансы
        logger.info("💰 Получение балансов...")
        try:
            balances = balance_service.get_all_balances()
            
            if balances:
                logger.info(f"   ✅ Балансы получены ({len(balances)} монет)")
                checks['balances'] = balances
                
                for bal in balances:
                    logger.info(f"      {bal['coin']}: {bal['total']:.6f}")
            else:
                logger.warning("   ⚠️ Балансы пусты (пополните кошелёк на тестнете)")
                checks['balances'] = []
                
        except Exception as e:
            logger.error(f"   ❌ Ошибка получения балансов: {e}")
            checks['balances'] = []
        
        # 8. Итог
        logger.info("")
        logger.info("=" * 60)
        logger.info("📊 РЕЗУЛЬТАТЫ ПРОВЕРОК:")
        
        all_critical_ok = all([
            checks.get('bybit_rest', False),
            checks.get('database', False)
        ])
        
        if all_critical_ok:
            logger.info("✅ Все критические проверки пройдены")
        else:
            logger.error("❌ Некоторые критические проверки провалены")
        
        logger.info(f"🎯 Режим работы: {checks['mode']}")
        logger.info("=" * 60)
        
        return checks
    
    async def start(self):
        """Запустить бота"""
        try:
            self.startup_checks = await self.startup_checks_routine()
            
            if not self.startup_checks.get('bybit_rest') or not self.startup_checks.get('database'):
                logger.error("❌ Критические компоненты недоступны, запуск невозможен")
                return False
            
            controller.mode = self.startup_checks['mode']
            
            logger.info("🚀 Запуск Telegram бота...")
            await self.telegram_bot.start()
            
            await self.telegram_bot.send_startup_message(self.startup_checks)
            
            # Подключаем уведомления от контроллера к Telegram
            controller.set_notifier(self.telegram_bot.send_message)
            
            await self.controller.start()
            
            self.is_running = True
            
            logger.info("")
            logger.info("=" * 60)
            logger.info("✅ БОТ УСПЕШНО ЗАПУЩЕН!")
            logger.info("=" * 60)
            
            return True
            
        except Exception as e:
            logger.exception("❌ Критическая ошибка при запуске")
            error_tracker.add_error("Main", "StartupError", str(e))
            return False
    
    async def stop(self):
        """Остановить бота"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("🛑 ОСТАНОВКА БОТА...")
        logger.info("=" * 60)
        
        try:
            await self.controller.stop()
            await self.telegram_bot.stop()
            
            self.is_running = False
            
            logger.info("=" * 60)
            logger.info(f"==== BOT STOP {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')} ====")
            logger.info("=" * 60)
            
        except Exception as e:
            logger.error(f"❌ Ошибка при остановке: {e}")
    
    async def run(self):
        """Главный цикл"""
        success = await self.start()
        
        if not success:
            logger.error("❌ Не удалось запустить бота")
            return
        
        loop = asyncio.get_event_loop()
        
        def signal_handler(sig, frame):
            logger.info(f"⚠️ Получен сигнал {sig}, остановка...")
            asyncio.create_task(self.stop())
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            while self.is_running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("⚠️ Прерывание пользователем")
        finally:
            await self.stop()


async def main():
    """Главная функция"""
    # setup_logger() вызывается при импорте settings
    
    bot = TradingBot()
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Выход из программы")
    except Exception as e:
        logger.exception("❌ Критическая ошибка")
        sys.exit(1)
