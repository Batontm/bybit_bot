"""
Конфигурация периодических задач APScheduler.

Все cron/interval-задачи описываются единой таблицей `_JOBS` (и
`_ARBITRAGE_JOBS` для опциональных арбитражных), что заменяет
~150 строк повторяющегося `scheduler.add_job(...)` на декларативную
структуру. Сами callback-методы остаются в `BotController`.

Использование (из `BotController.start()`):
    from .services.job_scheduler import setup_jobs
    setup_jobs(self.scheduler, self)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, List, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import TIMEZONE, logger
from config.trading_config import (
    ARBITRAGE_CHECK_INTERVAL,
    ARBITRAGE_ENABLED,
    BALANCE_UPDATE_INTERVAL,
    SIGNAL_CHECK_INTERVAL,
    TPSL_CHECK_INTERVAL,
)

if TYPE_CHECKING:  # избегаем циклического импорта
    from ..controller import BotController


# Структура записи: (job_id, name, controller_method_name, trigger)
JobSpec = Tuple[str, str, str, object]


def _build_jobs() -> List[JobSpec]:
    """Базовый набор задач — выполняется всегда."""
    return [
        # Торговый цикл
        ('check_signals',     'Проверка торговых сигналов',
         '_check_signals',    IntervalTrigger(seconds=SIGNAL_CHECK_INTERVAL)),
        ('update_balances',   'Обновление балансов',
         '_update_balances',  IntervalTrigger(seconds=BALANCE_UPDATE_INTERVAL)),
        ('check_tpsl',        'Проверка TP/SL',
         '_check_tpsl',       IntervalTrigger(seconds=TPSL_CHECK_INTERVAL)),
        ('sync_orders',       'Sync Orders/Trades',
         '_sync_orders_and_trades',  IntervalTrigger(seconds=60)),
        ('reconcile_orphans', 'Reconcile Orphan Orders',
         '_reconcile_orphan_orders', IntervalTrigger(hours=5)),

        # Управление открытыми позициями
        ('trailing_stop',     'Trailing Stop',
         '_update_trailing_stops',   IntervalTrigger(seconds=30)),
        ('check_breakeven',   'Breakeven Check',
         '_check_breakeven',  IntervalTrigger(seconds=30)),
        ('check_time_exit',   'Time Exit Check',
         '_check_time_exit',  IntervalTrigger(minutes=5)),
        ('check_dca',         'Smart DCA Check',
         '_check_dca',        IntervalTrigger(minutes=2)),

        # Защита позиций
        ('auto_sl',           'Auto SL Creation',
         '_auto_create_missing_sl',  IntervalTrigger(minutes=2)),
        ('emergency_sl',      'Emergency SL Watchdog',
         '_emergency_sl_watchdog',   IntervalTrigger(seconds=15)),

        # Дневные операции
        ('reset_daily',       'Сброс дневных лимитов',
         '_reset_daily_limits',
         CronTrigger(hour=0, minute=0, timezone=TIMEZONE)),
        ('daily_pnl_utc',     'Daily PnL UTC',
         '_update_daily_pnl_utc',
         CronTrigger(hour=0, minute=0, timezone='UTC')),

        # Динамические пары
        ('update_pairs',      'Обновление списка пар',
         '_update_market_pairs', IntervalTrigger(hours=1)),
    ]


def _build_arbitrage_jobs() -> List[JobSpec]:
    """Задачи арбитража — добавляются только при `ARBITRAGE_ENABLED`."""
    return [
        ('auto_arbitrage', 'Auto Arbitrage',
         '_auto_arbitrage', IntervalTrigger(seconds=ARBITRAGE_CHECK_INTERVAL)),
        ('update_arbitrage_funding', 'Update Arbitrage Funding',
         '_update_arbitrage_funding',
         CronTrigger(hour='0,8,16', minute=5, timezone='UTC')),
    ]


def _register(scheduler: AsyncIOScheduler, controller: 'BotController',
              jobs: Iterable[JobSpec]) -> None:
    for job_id, name, method_name, trigger in jobs:
        callback = getattr(controller, method_name)
        scheduler.add_job(
            callback,
            trigger=trigger,
            id=job_id,
            name=name,
            max_instances=1,
        )


def setup_jobs(scheduler: AsyncIOScheduler, controller: 'BotController') -> None:
    """Зарегистрировать все периодические задачи бота."""
    _register(scheduler, controller, _build_jobs())
    if ARBITRAGE_ENABLED:
        _register(scheduler, controller, _build_arbitrage_jobs())
    logger.info("📅 Планировщик настроен")
