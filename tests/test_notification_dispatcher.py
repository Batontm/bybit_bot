"""Тесты NotificationDispatcher."""
import pytest

from bot.services.notification_dispatcher import NotificationDispatcher


# ============= is_alert =============

@pytest.mark.parametrize("msg,expected", [
    ("⛔️ banned", True),
    ("❌ error", True),
    ("⚠️ warning", True),
    ("💸 slippage", True),
    ("✅ position opened", False),
    ("📊 stats", False),
    ("", False),
    (None, False),
])
def test_is_alert_classification(msg, expected):
    assert NotificationDispatcher.is_alert(msg) is expected


# ============= send routing =============

@pytest.fixture
def dispatcher_with_recorder():
    """Dispatcher + список собранных сообщений."""
    sent = []

    async def cb(message: str):
        sent.append(message)

    d = NotificationDispatcher()
    d.set_callback(cb)
    return d, sent


@pytest.mark.asyncio
async def test_send_normal_when_enabled(dispatcher_with_recorder):
    d, sent = dispatcher_with_recorder
    await d.send("hello")
    assert sent == ["hello"]


@pytest.mark.asyncio
async def test_send_alert_when_enabled(dispatcher_with_recorder):
    d, sent = dispatcher_with_recorder
    await d.send("⛔️ banned")
    assert sent == ["⛔️ banned"]


@pytest.mark.asyncio
async def test_normal_blocked_when_notifications_disabled(dispatcher_with_recorder):
    d, sent = dispatcher_with_recorder
    d.notifications_enabled = False
    await d.send("hello")
    assert sent == []


@pytest.mark.asyncio
async def test_alert_still_sent_when_only_normal_disabled(dispatcher_with_recorder):
    """Отключение notifications не глушит алерты."""
    d, sent = dispatcher_with_recorder
    d.notifications_enabled = False
    await d.send("⛔️ critical")
    assert sent == ["⛔️ critical"]


@pytest.mark.asyncio
async def test_alert_blocked_when_alerts_disabled(dispatcher_with_recorder):
    d, sent = dispatcher_with_recorder
    d.alerts_enabled = False
    await d.send("⛔️ critical")
    assert sent == []


@pytest.mark.asyncio
async def test_normal_still_sent_when_only_alerts_disabled(dispatcher_with_recorder):
    d, sent = dispatcher_with_recorder
    d.alerts_enabled = False
    await d.send("hello")
    assert sent == ["hello"]


@pytest.mark.asyncio
async def test_no_callback_does_not_raise():
    """Если callback не установлен — send не должен падать."""
    d = NotificationDispatcher()
    await d.send("hello")  # ничего не делает, не падает


@pytest.mark.asyncio
async def test_callback_exception_is_swallowed():
    """Исключение в callback не должно ронять логику бота."""
    async def boom(_msg):
        raise RuntimeError("simulated network error")

    d = NotificationDispatcher()
    d.set_callback(boom)
    await d.send("hello")  # должно тихо проглотиться
