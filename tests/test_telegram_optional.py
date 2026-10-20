"""Бот должен работать без Telegram (локальная проверка)."""
from bot.telegram_aiogram.bot import AiogramTelegramBot, telegram_configured


def test_placeholders_are_not_configured():
    assert not telegram_configured('', '')
    assert not telegram_configured('your_telegram_bot_token', 'your_telegram_chat_id')
    assert not telegram_configured('garbage', '123')
    assert telegram_configured('123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw', '123')


async def test_disabled_bot_starts_and_logs_messages():
    tg = AiogramTelegramBot()
    tg.token, tg.chat_id, tg.enabled = '', '', False
    await tg.start()
    assert tg.bot is None
    await tg.send_message("проверка")      # не падает
    await tg.send_startup_message({'mode': 'ACTIVE'})
    await tg.stop()
