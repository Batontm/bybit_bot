"""
Маршрутизация уведомлений.

Разделяет сообщения на две категории:
- alerts (критичные): начинаются с ⛔️ / ❌ / ⚠️ / 💸 — управляются `alerts_enabled`
- обычные уведомления — управляются `notifications_enabled`

Контроллер вызывает только `send(message)`, остальная логика инкапсулирована.
"""
from typing import Awaitable, Callable, Optional

from config.settings import logger


NotifyCallback = Callable[[str], Awaitable[None]]


class NotificationDispatcher:
    """Единая точка отправки уведомлений в Telegram (или иной канал)."""

    ALERT_PREFIXES = ("⛔️", "❌", "⚠️", "💸")

    def __init__(self, notifications_enabled: bool = True,
                 alerts_enabled: bool = True):
        self._callback: Optional[NotifyCallback] = None
        self.notifications_enabled = notifications_enabled
        self.alerts_enabled = alerts_enabled

    def set_callback(self, callback: NotifyCallback) -> None:
        self._callback = callback

    @classmethod
    def is_alert(cls, message: str) -> bool:
        if not message:
            return False
        return message.startswith(cls.ALERT_PREFIXES)

    async def send(self, message: str) -> None:
        """Отправить уведомление (с учётом флагов alerts/notifications)."""
        try:
            if self.is_alert(message):
                if not self.alerts_enabled:
                    return
            else:
                if not self.notifications_enabled:
                    return
        except Exception:
            pass

        if self._callback is None:
            return

        try:
            await self._callback(message)
        except Exception as e:
            logger.error(f"❌ Ошибка отправки уведомления: {e}")
