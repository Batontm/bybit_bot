"""
Основной класс Telegram бота
"""
from typing import Optional
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters
)
from config.settings import logger
from config.api_config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from .handlers import TelegramHandlers
from . import formatters, keyboards


class TelegramBot:
    """Telegram интерфейс для управления ботом"""
    
    def __init__(self, controller=None):
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.controller = controller
        self.app: Optional[Application] = None
        self.handlers = TelegramHandlers(controller)
    
    async def start(self):
        """Запустить Telegram бота"""
        self.app = Application.builder().token(self.token).build()
        
        # Регистрация обработчиков
        self.app.add_handler(CommandHandler("start", self.handlers.cmd_start))
        self.app.add_handler(CallbackQueryHandler(self.handlers.button_handler))
        
        # Обработчик текстовых кнопок (ReplyKeyboard)
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.handlers.text_handler
        ))
        
        # Запуск
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        
        logger.info("✅ Telegram бот запущен")
    
    async def stop(self):
        """Остановить Telegram бота"""
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            logger.info("🛑 Telegram бот остановлен")
    
    async def send_message(self, text: str, reply_markup=None):
        """Отправить сообщение пользователю"""
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"❌ Ошибка отправки сообщения: {e}")
    
    async def send_startup_message(self, startup_checks: dict):
        """Отправить приветственное сообщение при запуске с ПОСТОЯННОЙ клавиатурой"""
        text = formatters.format_startup_message(startup_checks)
        
        # Используем постоянную клавиатуру внизу экрана
        reply_keyboard = keyboards.get_main_menu_reply_keyboard()
        
        await self.app.bot.send_message(
            chat_id=self.chat_id,
            text=text + "\n\n👇 Выберите действие:",
            reply_markup=reply_keyboard,
            parse_mode='HTML'
        )


# Функция для создания экземпляра
def create_telegram_bot(controller=None) -> TelegramBot:
    """Создать экземпляр Telegram бота"""
    return TelegramBot(controller)
