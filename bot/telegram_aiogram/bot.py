from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional, Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config.api_config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from config.settings import logger

from .handlers import create_router


MAX_MESSAGES_PER_MINUTE = 20


def telegram_configured(token: Optional[str], chat_id: Optional[str]) -> bool:
    """Токен вида '123456:ABC...' и chat_id заданы (не заглушки из .env.example)."""
    if not token or not chat_id:
        return False
    if token.startswith('your_') or str(chat_id).startswith('your_'):
        return False
    head, _, tail = token.partition(':')
    return head.isdigit() and len(tail) >= 20


class AiogramTelegramBot:
    def __init__(self, controller=None):
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.controller = controller

        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self._polling_task: Optional[asyncio.Task] = None
        self._msg_timestamps: deque = deque()
        # Без Telegram бот работает дальше, а уведомления пишутся в лог
        # (удобно для локальной проверки на компьютере).
        self.enabled = telegram_configured(self.token, self.chat_id)

    async def start(self) -> None:
        if not self.enabled:
            logger.warning("⚠️ Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — "
                           "бот работает без Telegram, уведомления пишутся в лог")
            return
        try:
            self.bot = Bot(
                token=self.token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
        except Exception as e:
            logger.error(f"❌ Telegram: неверный токен ({e}) — работаю без Telegram")
            self.enabled = False
            self.bot = None
            return
        self.dp = Dispatcher()
        self.dp.include_router(create_router(self.controller))

        try:
            from aiogram.types import BotCommand
            await self.bot.set_my_commands([
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="health", description="Дашборд: баланс, позиции, риск"),
                BotCommand(command="positions", description="Позиции и их закрытие"),
                BotCommand(command="signals", description="Сигналы: последний проход воронки"),
                BotCommand(command="regime", description="Светофор BTC 4H"),
                BotCommand(command="pnl", description="Статистика PnL"),
                BotCommand(command="arbitrage", description="Арбитраж"),
                BotCommand(command="settings", description="Настройки"),
                BotCommand(command="service", description="Сверка, ошибки, логи"),
            ])
        except Exception as e:
            logger.debug(f"set_my_commands: {e}")

        self._polling_task = asyncio.create_task(self.dp.start_polling(self.bot))
        logger.info("✅ Telegram (aiogram) бот запущен")

    async def stop(self) -> None:
        if not self.enabled:
            return
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass

        if self.bot:
            await self.bot.session.close()

        logger.info("🛑 Telegram (aiogram) бот остановлен")

    async def send_message(self, text: str, reply_markup=None) -> None:
        if not self.bot:
            logger.info(f"📣 [уведомление] {text}")
            return
        if not self.chat_id:
            logger.error("❌ TELEGRAM_CHAT_ID не установлен")
            return

        # Rate limiter: max N messages per 60 seconds
        now = time.monotonic()
        while self._msg_timestamps and self._msg_timestamps[0] < now - 60:
            self._msg_timestamps.popleft()
        if len(self._msg_timestamps) >= MAX_MESSAGES_PER_MINUTE:
            logger.warning(f"⚠️ Rate limit: {MAX_MESSAGES_PER_MINUTE} msg/min exceeded, dropping message")
            return
        self._msg_timestamps.append(now)

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            # Текст с «<» или «&» (например, из ответа биржи) ломает HTML-разметку —
            # тогда отправляем как обычный текст, чтобы уведомление не потерялось
            if "parse entities" in str(e).lower() or "unsupported start tag" in str(e).lower():
                try:
                    await self.bot.send_message(chat_id=self.chat_id, text=text,
                                                reply_markup=reply_markup, parse_mode=None)
                    return
                except Exception as e2:
                    e = e2
            logger.error(f"❌ Ошибка отправки сообщения: {e}")

    async def send_startup_message(self, startup_checks: dict) -> None:
        from bot.controller import BOT_VERSION
        from config.trading_config import ENTRY_STRATEGY
        lines = [
            f"🤖 <b>Бот запущен</b> ({BOT_VERSION})",
            f"Режим: {startup_checks.get('mode', 'UNKNOWN')} | Стратегия: {ENTRY_STRATEGY}",
        ]
        # Те же кнопки, что и в главном меню: у каждой есть обработчик
        from .handlers import _main_menu_kb
        await self.send_message("\n".join(lines), reply_markup=_main_menu_kb().as_markup())


def create_telegram_bot(controller=None) -> AiogramTelegramBot:
    return AiogramTelegramBot(controller)
