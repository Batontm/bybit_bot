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


class AiogramTelegramBot:
    def __init__(self, controller=None):
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.controller = controller

        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self._polling_task: Optional[asyncio.Task] = None
        self._msg_timestamps: deque = deque()

    async def start(self) -> None:
        self.bot = Bot(
            token=self.token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self.dp.include_router(create_router(self.controller))

        self._polling_task = asyncio.create_task(self.dp.start_polling(self.bot))
        logger.info("✅ Telegram (aiogram) бот запущен")

    async def stop(self) -> None:
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
            logger.error(f"❌ Ошибка отправки сообщения: {e}")

    async def send_startup_message(self, startup_checks: dict) -> None:
        lines = [
            "🤖 Bybit bot started",
            f"Mode: {startup_checks.get('mode', 'UNKNOWN')}",
        ]
        kb = InlineKeyboardBuilder()
        kb.button(text="💼 Статус", callback_data="tg:health")
        kb.button(text="📜 Логи", callback_data="tg:logs")
        kb.button(text="⚠️ Ошибки", callback_data="tg:errors")
        kb.button(text="🔁 Сверка", callback_data="tg:reconcile")
        kb.button(text="📈 PnL", callback_data="tg:pnl")
        kb.button(text="💹 Арбитраж", callback_data="tg:arbitrage")
        kb.button(text="🔔 Уведомления", callback_data="tg:notifications")
        kb.button(text="🚨 Алерты", callback_data="tg:alerts")
        kb.adjust(2)

        await self.send_message("\n".join(lines), reply_markup=kb.as_markup())


def create_telegram_bot(controller=None) -> AiogramTelegramBot:
    return AiogramTelegramBot(controller)
