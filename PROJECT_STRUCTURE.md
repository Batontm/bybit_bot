# Bybit Trading Bot — Полная документация проекта

> **Последнее обновление:** 2026-02-09 v2.1.0

## 📋 Обзор

Автоматизированный торговый бот для **спотовой торговли на Bybit** с AI-анализом через Perplexity, управлением через Telegram (aiogram) и спот-фьючерсным арбитражем.

| Параметр | Значение |
|----------|----------|
| **Язык** | Python 3.12+ |
| **Биржа** | Bybit Demo Trading (спот + фьючерсы) |
| **API URL** | `https://api-demo.bybit.com` |
| **AI** | Perplexity API (модель Sonar) + Ollama (qwen2.5:1.5b) |
| **Telegram** | aiogram 3.x (inline-кнопки, callback) |
| **БД** | SQLite (`data/bot.db`) — WAL mode, thread-safe |
| **Сервер** | 217.12.37.42:58291 (root, SSH alias: `bybit-bot`) |
| **Таймзона** | Europe/Kaliningrad (UTC+2) |

### Что делает бот

1. **Светофор (Market Regime)** — BTC 4H: если close > EMA200, RSI14 >= 50 и ATR не в панике (< 2x avg) → ЗЕЛЕНЫЙ, иначе КРАСНЫЙ
2. **Мониторинг пар** — фиксированные (BTC, ETH, SOL) + динамические Top Gainers (сканер каждый час)
3. **Pre-filter** — Python-анализ (RSI, EMA, MACD, Volume) — бесплатно, отсеивает слабых кандидатов
4. **AI-анализ** — Perplexity API для топ-2 кандидатов — платно ($0.02/запрос)
5. **Авто-вход** — если AI score >= 80 и все риск-лимиты в норме
6. **Защита позиций** — TP/SL ордера (ATR-based), Trailing Stop, Breakeven, Time Exit
7. **Пирамидинг** — первоначальный вход 65%, докупка 35% при просадке -0.5%
8. **Smart DCA** — докупка при просадке -1.5% с учётом AI-анализа (макс. 2 докупки)
9. **Auto-SL** — автоматическое создание SL для незащищённых позиций
10. **Спот-Фьючерс Арбитраж** — авто-funding через SHORT фьючерсов (только BTC/ETH — collateral whitelist)
11. **Slippage Protection** — бан пары на 24ч при 3+ превышениях проскальзывания
12. **Telegram UI** — полное управление ботом через inline-кнопки (aiogram 3.x)
13. **Уведомления и Алерты** — раздельное управление (ON/OFF) через Telegram
14. **Panic Sell** — экстренная кнопка в Telegram: отмена всех ордеров, закрытие позиций, пауза бота
15. **Reconcile Orphan Orders** — сверка ордеров биржи и БД, отмена зомби-ордеров
16. **Dust Balance Handling** — автоматическое закрытие позиций с пылевым остатком в БД
17. **Telegram Rate Limiter** — ограничение 20 msg/min для защиты от бана
18. **Systemd Service** — автоперезапуск бота при падении (`bybit-bot.service`)

---

## 📁 Структура проекта

```
bybit_bot/
├── bot/                              # Основная логика бота
│   ├── main.py                       # Точка входа, стартовые проверки
│   ├── controller.py                 # Планировщик задач (APScheduler), главный цикл
│   ├── perplexity_client.py          # AI-анализ через Perplexity (sonar)
│   ├── ollama_client.py              # AI-анализ через Ollama (qwen2.5:1.5b, локально)
│   ├── llm_router.py                 # Маршрутизатор LLM провайдеров
│   ├── error_tracker.py              # Кольцевой буфер ошибок (deque, 200 записей)
│   │
│   ├── services/                     # Бизнес-логика (singleton через глобальные экземпляры)
│   │   ├── market_regime_service.py  # "Светофор" — BTC 4H: EMA200 + RSI14 + ATR spike filter
│   │   ├── analysis_service.py       # AI-анализ пар + кэширование
│   │   ├── trading_service.py        # Открытие позиций, TP/SL, DCA, пирамидинг
│   │   ├── position_service.py       # Закрытие, Trailing, Breakeven, Auto-SL, Time Exit, Reconcile, Dust
│   │   ├── balance_service.py        # Балансы USDT и монет (UNIFIED account)
│   │   ├── prefilter_service.py      # Технический pre-filter (RSI, EMA, MACD, Volume)
│   │   ├── indicators_service.py     # Индикаторы: RSI, EMA, MACD, ATR, Bollinger Bands
│   │   ├── scanner_service.py        # Сканер Top Gainers (mainnet данные)
│   │   ├── arbitrage_service.py      # Спот-Фьючерс арбитраж (funding rates, collateral whitelist)
│   │   ├── stats_service.py          # Статистика: PnL по дням/парам, LLM-отчёты
│   │   ├── chart_service.py          # Генерация PNG-графиков (matplotlib)
│   │   └── websocket_service.py      # Цены реалтайм (pybit WebSocket + REST fallback)
│   │
│   ├── db/                           # Работа с базой данных (SQLite, WAL mode)
│   │   ├── connection.py             # Singleton подключение (thread-safe)
│   │   ├── trades_repo.py            # CRUD: позиции, ордера, сделки
│   │   ├── pnl_repo.py              # PnL по дням и парам
│   │   ├── daily_pnl_repo.py        # Дневной PnL (upsert по date_utc)
│   │   ├── llm_requests_repo.py     # Логи запросов к Perplexity/Ollama
│   │   └── arbitrage_repo.py        # Арбитражные позиции
│   │
│   ├── telegram_aiogram/            # Telegram интерфейс (АКТИВНЫЙ, aiogram 3.x)
│   │   ├── bot.py                   # AiogramTelegramBot: start/stop/send_message + rate limiter
│   │   ├── handlers.py              # Все callback-обработчики + Panic Sell (~740 строк)
│   │   └── __init__.py
│   │
│   └── telegram_client/             # Telegram (УСТАРЕВШИЙ, python-telegram-bot)
│       ├── bot.py, handlers.py, keyboards.py, formatters.py
│
├── config/
│   ├── settings.py                  # TIMEZONE, TESTNET, LOG_LEVEL, DB_PATH
│   ├── api_config.py                # API ключи, get_pybit_kwargs(), BYBIT_DEMO
│   └── trading_config.py            # Все параметры торговли и риск-менеджмента
│
├── scripts/                         # Утилиты и диагностика
│   ├── check_balance.py, sell_all.py, fix_balances.py, db_debug.py ...
│
├── data/bot.db                      # SQLite база данных
├── logs/bot.log, run.log            # Логи
├── .env                             # Переменные окружения (НЕ в git)
├── bybit-bot.service                # Systemd unit (автоперезапуск)
├── run_bot.sh / stop_bot.sh         # Скрипты запуска/остановки (устаревшие, используется systemd)
└── PROJECT_STRUCTURE.md             # Эта документация
```

---

## 🔧 Ключевые сервисы — подробная логика

### `main.py` — Точка входа

При запуске выполняет стартовые проверки:
1. Валидация конфигурации (API ключи, токены)
2. Проверка Bybit REST API (get_server_time)
3. Тестовый ордер (Limit Buy BTC → Cancel) — проверка торговых прав
4. Проверка WebSocket эндпоинтов
5. Проверка Perplexity API
6. Проверка БД (количество таблиц)
7. Получение балансов
8. Запуск Telegram бота (aiogram)
9. Запуск контроллера (APScheduler)

### `controller.py` — BotController (планировщик)

**Режимы:** `ACTIVE` | `RISK_ONLY` | `PAUSED`

**Свойства:**
- `auto_trading_enabled` — разрешение на авто-торговлю
- `scanner_enabled` — включение сканера Top Gainers
- `notifications_enabled` / `alerts_enabled` — раздельное управление уведомлениями
- `fixed_pairs` — [BTCUSDT, ETHUSDT, SOLUSDT]
- `dynamic_pairs` — от сканера
- `dynamic_pairs_data` — {symbol: timestamp} для 24ч памяти

**Задачи планировщика:**

| Задача | Интервал | Метод |
|--------|----------|-------|
| Проверка сигналов | 10 мин | `_check_signals()` |
| Обновление балансов | 1 мин | `_update_balances()` |
| Проверка TP/SL | 10 сек | `_check_tpsl()` |
| Обновление цен | 30 сек | `_update_positions_prices()` |
| Sync Orders | 60 сек | `_sync_orders_and_trades()` |
| Reconcile Orphans | 5 часов | `_reconcile_orphan_orders()` |
| Trailing Stop | 30 сек | `_update_trailing_stops()` |
| Breakeven | 30 сек | `_check_breakeven()` |
| Time Exit | 5 мин | `_check_time_exit()` |
| Smart DCA | 2 мин | `_check_dca()` |
| Auto-SL | 2 мин | `_auto_create_missing_sl()` |
| Emergency SL | 15 сек | `_emergency_sl_watchdog()` |
| Сканер пар | 1 час | `_update_market_pairs()` |
| Сброс лимитов | 00:00 | `_reset_daily_limits()` |
| Авто-арбитраж | 1 час | `_auto_arbitrage()` |
| Funding Update | 0:00, 8:00, 16:00 | `_update_arbitrage_funding()` |

**Логика `_check_signals()`:**
1. Проверка режима (не PAUSED, auto_trading включён)
2. **Светофор**: `market_regime_service.is_trading_allowed()` — КРАСНЫЙ → пропуск
3. Сбор пар (fixed + dynamic), исключение пар с открытыми позициями и забаненных
4. **Pre-filter**: `prefilter_service.scan_and_filter(pairs, top_n=2)`
5. AI-анализ каждого кандидата через Perplexity
6. `should_enter_trade(analysis)` — score >= 80
7. `check_risk_limits()` — лимиты позиций, дневной лосс
8. `trading_service.open_position(pair, analysis)` → TP/SL ордера
9. Проверка slippage → бан пары при 3+ превышениях
10. Telegram уведомление

**`panic_sell_all()` — Экстренная остановка (v2.1):**
1. Устанавливает режим `PAUSED`, отключает `auto_trading_enabled`
2. Отменяет ВСЕ ордера на бирже (spot + linear)
3. Закрывает ВСЕ открытые спот-позиции по рынку
4. Закрывает ВСЕ арбитражные позиции
5. Помечает все DB-ордера как `Cancelled`
6. Вызывается через Telegram кнопку 🛑 PANIC SELL (двухэтапное подтверждение)

**Логика `_auto_arbitrage()`:**
- В КРАСНОМ режиме: усиленный арбитраж (до 8 позиций, 30% депозита)
- Сканирование funding rates → Spot LONG + Futures SHORT
- Динамический размер: `remaining_allocation / remaining_slots`
- **Collateral whitelist (v2.1):** только BTC/ETH (collateral ratio >= 0.9)

### `market_regime_service.py` — Светофор

Анализ BTC на 4H (mainnet данные):
- 250 свечей BTCUSDT 4H → EMA200 + RSI14 + ATR spike
- **ЗЕЛЕНЫЙ**: `close > EMA200` И `RSI14 >= 50` И `ATR_ratio < 2.0`
- **КРАСНЫЙ**: любое условие не выполнено
- **ATR spike filter (v2.1):** если текущий ATR(14) > 2x от ATR предыдущих свечей → блокировка (паника/крэш)
- Кэш: 5 минут

### `position_service.py` — Управление позициями

| Метод | Описание |
|-------|----------|
| `close_position(id, reason)` | Отмена ордеров → проверка баланса → Market Sell → PnL |
| `update_trailing_stops()` | При прибыли > 3%: SL = цена - 1.5% |
| `check_breakeven()` | При прибыли >= 1%: SL = entry + 0.1% |
| `check_time_exit()` | Закрытие позиций старше 8ч с движением < 0.5% |
| `auto_create_missing_sl()` | SL для незащищённых позиций, синхронизация qty |
| `emergency_sl_watchdog()` | Аварийный SL |
| `sync_orders_and_trades()` | Синхронизация статусов ордеров с биржей |
| `reconcile_orphan_orders()` | **(v2.1)** Сверка ордеров биржи ↔ БД, отмена зомби-ордеров |
| `_get_actual_balance(pair)` | Реальный баланс монеты на бирже |
| `_format_qty(pair, qty)` | math.floor, правильные decimals |

**Логика `close_position()`:**
1. Отмена ордеров в БД и на бирже
2. Получение текущей цены
3. Проверка реального баланса — если < qty * 0.99 → продаём остаток
4. **Dust handling (v2.1):** если продажа остатка → ошибка min order size (170217) → закрываем только в БД
5. Market Sell → при ошибке 170131 повторная попытка с actual_balance
6. Расчёт PnL, запись в БД

**Логика `reconcile_orphan_orders()` (v2.1):**
1. Получает открытые ордера с биржи и из БД
2. Ордера на бирже, но НЕ в БД → отмена на бирже (зомби-ордера)
3. Ордера в БД, но НЕ на бирже → проверка истории → обновление статуса в БД
4. Запускается каждые 5 часов через планировщик

### `trading_service.py` — Торговля

| Метод | Описание |
|-------|----------|
| `open_position(pair, analysis)` | Возвращает `(position, error_message)` |
| `add_to_position(id, analysis)` | Smart DCA — докупка |
| `check_risk_limits()` | Макс. позиций, дневной лосс, макс. пар |
| `should_enter_trade(analysis)` | Score >= 80 + тренд OK |
| `_calculate_position_size()` | risk% * deposit / SL_distance * (score/80) |

**Логика `open_position()`:**
1. Тренд-фильтр (EMA20 > EMA50)
2. ATR-based TP/SL (или фиксированные fallback)
3. Position sizing: `MAX_RISK_PER_TRADE * deposit / SL_distance`
4. Корректировка по AI score: `size * (score / 80)`, 0.5x–1.2x
5. Пирамидинг: первый вход = 65%
6. Market Buy → TP/SL ордера → возврат `(position, None)` или `(None, "ошибка")`

### `prefilter_service.py` — Технический фильтр

**Стратегия "Воронка":** Python фильтр → Perplexity (только лучшие).

Критерии: RSI 25–70, Volume >= 1.2x среднего.

Scoring (0–100): RSI 30–45: +20 | EMA BULLISH: +15 | MACD BULLISH: +10 | Volume 1.5x: +15

### `indicators_service.py` — Технические индикаторы

| Индикатор | Метод | Описание |
|-----------|-------|----------|
| RSI | `calculate_rsi(closes, 14)` | 0–100 |
| EMA | `calculate_ema(closes, period)` | Exponential Moving Average |
| MACD | `calculate_macd(closes, 12, 26, 9)` | line, signal, histogram |
| ATR | `calculate_atr(highs, lows, closes, 14)` | волатильность |
| Bollinger | `calculate_bollinger_bands(closes, 20, 2.0)` | upper, middle, lower |
| `analyze()` | Полный анализ | Все индикаторы + overall_score/signal |

### `scanner_service.py` — Сканер Top Gainers

1. Тикеры с **mainnet** (реальные данные)
2. Фильтр: USDT пары, без стейблов и leverage-токенов
3. На demo: проверка доступности пары
4. Критерии: рост 1.5–100% за 24ч, объём > 1M USDT
5. 24-часовая память в контроллере

### `arbitrage_service.py` — Спот-Фьючерс арбитраж

**Стратегия:** Spot LONG + Futures SHORT → заработок на funding rate.

| Метод | Описание |
|-------|----------|
| `scan_funding_rates()` | Сканирование funding для топ-ликвидных пар |
| `open_arbitrage(pair, size_usd)` | Spot Buy + Futures Short |
| `close_arbitrage(position_id)` | Spot Sell + Futures Close |
| `update_funding_for_all()` | Обновление накопленного funding |
| `get_dashboard()` | Дашборд: позиции, PnL, статистика |

**Красный режим:** макс. 8 позиций, 30% от стейблов, мин. $50/позиция.

### `llm_router.py` — Маршрутизатор AI

| Режим | Описание |
|-------|----------|
| `PERPLEXITY_ONLY` | Только Perplexity (текущий) |
| `LOCAL_ONLY` | Только Ollama |
| `HYBRID` | Perplexity → Ollama fallback |
| `PREFER_LOCAL` | Ollama → Perplexity fallback |

### `stats_service.py` — Статистика

- `get_pnl_by_pairs_report(days)` — PnL по парам (топ-10)
- `get_pnl_by_days_report(days)` — PnL по дням
- `get_llm_stats_report(period)` — Perplexity: запросы, стоимость, бюджет
- `get_positions_status_report()` — Текущие открытые позиции

### `chart_service.py` — Графики

PNG-графики: свечи + TP/SL уровни + стрелки BUY/SELL (matplotlib, Agg backend).

### `error_tracker.py` — Трекер ошибок

Кольцевой буфер (deque, 200 записей). Каждая запись: timestamp, module, error_type, message, traceback.

---

## 🤖 Telegram интерфейс (aiogram 3.x)

### Архитектура

**`bot/telegram_aiogram/bot.py`** — класс `AiogramTelegramBot`:
- `start()` — создание Bot, Dispatcher, запуск polling
- `stop()` — остановка polling, закрытие сессии
- `send_message(text, reply_markup)` — отправка сообщения в TELEGRAM_CHAT_ID
- `send_startup_message(checks)` — стартовое сообщение с главным меню

**`bot/telegram_aiogram/handlers.py`** — все обработчики (~709 строк):
- Авторизация: `TELEGRAM_ALLOWED_USER_IDS` из `.env`
- Команды: `/start`, `/menu`
- Callback-обработчики для inline-кнопок

### Главное меню (Inline Keyboard)

| Кнопка | Callback | Описание |
|--------|----------|----------|
| 💼 Статус | `tg:health` | Балансы, позиции, рыночный режим |
| 🚦 Режим | `tg:regime` | Светофор: BTC 4H EMA200/RSI14 |
| ⚠️ Ошибки | `tg:errors` | Последние 20 ошибок |
| 🔁 Сверка | `tg:reconcile` | Запуск reconcile_orphan_orders |
| 📈 PnL | `tg:pnl` | PnL за 7/30 дней или всё время |
| 💹 Арбитраж | `tg:arbitrage` | Сканер, открытие арбитража |
| 🔔 Уведомления | `tg:notifications` | Вкл/выкл уведомлений |
| 🚨 Алерты | `tg:alerts` | Вкл/выкл критичных алертов |

### Статус (`tg:health`)

- Рыночный режим (Светофор)
- Балансы (Bybit Unified): монета, всего, к_выводу, не_к_выводу
- Открытые позиции: пара, кол-во, вход, цена, PnL%

### PnL (`tg:pnl`)

Подменю: 7 дней / 30 дней / Всё время → дата, PnL, количество сделок

### Арбитраж (`tg:arbitrage`)

- Список возможностей (funding rates)
- Кнопки открытия арбитража для каждой пары
- Дашборд: открытые позиции, funding PnL

### Уведомления vs Алерты

- **Уведомления** — обычные (открытие/закрытие позиций, сканер)
- **Алерты** — критичные (⛔️❌⚠️💸) — управляются отдельно

---

## 💾 Схема базы данных

### Таблицы

| Таблица | Назначение |
|---------|------------|
| `positions` | Открытые/закрытые позиции |
| `orders` | Ордера (entry, TP, SL) |
| `trades` | Исполненные сделки |
| `pnl_history` | История PnL по дням/парам |
| `daily_pnl` | Дневной PnL (gross, net, commission, slippage) |
| `llm_requests` | Логи запросов к Perplexity/Ollama |
| `settings` | Настройки бота (key-value) |
| `arbitrage_positions` | Арбитражные позиции |

### positions

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | INTEGER PK | ID позиции |
| `pair` | TEXT | Торговая пара |
| `entry_price` | REAL | Цена входа |
| `avg_entry_price` | REAL | Средняя цена (после DCA) |
| `quantity` | REAL | Количество монет |
| `current_price` | REAL | Текущая цена |
| `tp_price` | REAL | Take Profit |
| `sl_price` | REAL | Stop Loss |
| `status` | TEXT | OPEN / CLOSED |
| `opened_at` / `closed_at` | TEXT | Время |
| `unrealized_pnl` | REAL | Нереализованный PnL |
| `unrealized_pnl_percent` | REAL | PnL в % |
| `realized_pnl` | REAL | Реализованный PnL |
| `dca_count` | INTEGER | Количество DCA |
| `breakeven_activated` | INTEGER | Флаг breakeven |

### orders

| Поле | Тип | Описание |
|------|-----|----------|
| `order_id` | TEXT PK | ID ордера на бирже |
| `pair` | TEXT | Торговая пара |
| `side` | TEXT | Buy / Sell |
| `order_type` | TEXT | Market / Limit |
| `price` | REAL | Цена ордера |
| `quantity` / `filled_quantity` | REAL | Кол-во / исполнено |
| `avg_fill_price` | REAL | Средняя цена исполнения |
| `status` | TEXT | New / Filled / Cancelled |
| `is_tp` / `is_sl` | INTEGER | Флаги TP/SL |
| `position_id` | INTEGER FK | Связь с позицией |

### trades

| Поле | Тип | Описание |
|------|-----|----------|
| `trade_id` | TEXT PK | ID сделки |
| `order_id` | TEXT FK | Связь с ордером |
| `position_id` | INTEGER FK | Связь с позицией |
| `pair` | TEXT | Пара |
| `side` | TEXT | Buy / Sell |
| `price` / `quantity` | REAL | Цена / количество |
| `fee` / `fee_asset` | REAL / TEXT | Комиссия |

### settings (key-value)

| Ключ | Описание |
|------|----------|
| `scanner_enabled` | Сканер (0/1) |
| `auto_trading_enabled` | Авто-торговля (0/1) |
| `notifications_enabled` | Уведомления (0/1) |
| `alerts_enabled` | Алерты (0/1) |
| `ban_pair_{PAIR}` | Бан пары до datetime |

---

## ⚙️ Конфигурация

### `config/api_config.py`

| Переменная | Описание |
|------------|----------|
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | API ключи (из .env) |
| `BYBIT_BASE_URL` | `https://api-demo.bybit.com` |
| `BYBIT_TESTNET` | True если "testnet" в URL |
| `BYBIT_DEMO` | True если "demo" в URL |
| `BYBIT_ACCOUNT_TYPE` | `UNIFIED` |
| `get_pybit_kwargs()` | → `{demo: True}` / `{testnet: True}` / `{testnet: False}` |
| `get_pybit_ws_public_kwargs()` | demo → `{testnet: False}` (mainnet для public WS) |
| `TELEGRAM_ALLOWED_USER_IDS` | Список разрешённых user ID |

### `config/trading_config.py`

#### Риск-менеджмент

| Параметр | Значение | Описание |
|----------|----------|----------|
| `MAX_RISK_PER_TRADE` | 0.5% | Риск на сделку |
| `MAX_TOTAL_RISK` | 5% | Общий риск |
| `MAX_DAILY_LOSS` | 3% | Дневной стоп-лосс |
| `MAX_OPEN_POSITIONS` | 5 | Макс. позиций |
| `MAX_ACTIVE_PAIRS` | 3 | Макс. пар |
| `MAX_NEW_TRADES_PER_DAY` | 15 | Макс. сделок/день |
| `MAX_CONSECUTIVE_LOSSES` | 3 | Пауза после 3 лоссов |
| `MAX_SLIPPAGE_PERCENT` | 0.2% | Макс. проскальзывание |

#### TP/SL (ATR-based)

| Параметр | Значение |
|----------|----------|
| `ATR_BASED_TPSL_ENABLED` | True |
| `ATR_PERIOD` | 14 |
| `ATR_TIMEFRAME` | "60" (1H) |
| `ATR_TP_MULTIPLIER` | 3.0 (TP = entry + ATR × 3) |
| `ATR_SL_MULTIPLIER` | 1.0 (SL = entry - ATR × 1) |
| `DEFAULT_TP_PERCENT` | +2% (fallback) |
| `DEFAULT_SL_PERCENT` | -1% (fallback) |

#### Trailing Stop & Breakeven

| Параметр | Значение |
|----------|----------|
| `TRAILING_ACTIVATION` | +3% прибыли |
| `TRAILING_STEP` | 1.5% от цены |
| `BREAKEVEN_TRIGGER_PERCENT` | +1% |
| `BREAKEVEN_BUFFER` | +0.1% над входом |

#### Пирамидинг & DCA

| Параметр | Значение |
|----------|----------|
| `INITIAL_POSITION_PERCENT` | 65% |
| `PYRAMIDING_TRIGGER` | -0.5% |
| `PYRAMIDING_ADD_PERCENT` | 35% |
| `DCA_TRIGGER_PERCENT` | -1.5% |
| `DCA_MAX_ENTRIES` | 2 |
| `DCA_MIN_SCORE` | 50 |
| `DCA_POSITION_MULTIPLIER` | 0.5 |

#### Time Exit

| Параметр | Значение |
|----------|----------|
| `MAX_POSITION_HOURS` | 8 |
| `STALE_MOVE_THRESHOLD` | 0.5% |

#### Тренд-фильтр

LONG только если EMA20 > EMA50 (`TREND_FILTER_ENABLED = True`)

#### Арбитраж

| Параметр | Значение |
|----------|----------|
| `ARBITRAGE_ENABLED` | True |
| `ARBITRAGE_CHECK_INTERVAL` | 3600 сек |
| `ARBITRAGE_MIN_FUNDING_RATE` | 0.01% |
| `ARBITRAGE_MAX_POSITIONS` | 3 (зелёный) / 8 (красный) |
| `ARBITRAGE_POSITION_SIZE_USD` | $100 |
| `ARBITRAGE_RED_TOTAL_ALLOCATION_PCT` | 30% |
| `ARBITRAGE_RED_MIN_POSITION_SIZE_USD` | $50 |

#### Perplexity AI

| Параметр | Значение |
|----------|----------|
| `MAX_LLM_COST_PER_MONTH` | $30 |
| `MAX_LLM_REQUESTS_PER_DAY` | 100 |
| `LLM_PROVIDER_MODE` | PERPLEXITY_ONLY |
| `THRESHOLD_AUTO_TRADE` | 80 |
| `THRESHOLD_BUY` | 70 |
| `THRESHOLD_WAIT` | 50 |

---

## 🔄 Логика работы — диаграммы

### Главный цикл

```
_check_signals() [каждые 10 мин]
│
├─ Режим PAUSED? → пропуск
├─ Авто-торговля выкл? → пропуск
│
├─ 🚦 СВЕТОФОР: is_trading_allowed()
│  └─ КРАСНЫЙ → пропуск новых сделок
│
├─ Сбор пар: fixed + dynamic
│  └─ Исключение: с позициями + забаненные
│
├─ PRE-FILTER: scan_and_filter(pairs, top_n=2)
│  └─ RSI 25-70, Volume >= 1.2x, EMA, MACD → Score → топ-2
│
├─ Для каждого кандидата:
│  ├─ AI: analyze_pair() → score, signal, target, SL
│  ├─ score >= 80? → ДА
│  ├─ risk_limits OK? → ДА
│  ├─ open_position() → Market Buy + TP/SL
│  └─ Telegram уведомление
│
└─ Задержка 2 сек между парами
```

### Жизненный цикл позиции

```
ОТКРЫТИЕ (Market Buy + TP/SL)
│
├─ [30 сек] update_prices → PnL
├─ [30 сек] check_breakeven → SL = entry + 0.1% при +1%
├─ [30 сек] trailing_stops → SL = цена - 1.5% при +3%
├─ [2 мин] check_dca → докупка при -1.5%
├─ [2 мин] auto_sl → создание SL если нет
├─ [5 мин] time_exit → закрытие при >8ч + <0.5% движения
├─ [15 сек] emergency_sl → аварийный SL
│
└─ ЗАКРЫТИЕ: TP | SL | Trailing | Time Exit | Ручное (Telegram)
```

---

## 🚀 Деплой

### Локально
```bash
cd /Users/lexa/Documents/Python/bybit_bot
source .venv/bin/activate
python -m bot.main
```

### На сервер
```bash
scp -r bot/ config/ bybit-bot:/root/bybit_bot/
ssh bybit-bot "pkill -f 'bot/main.py'; sleep 2; cd /root/bybit_bot && nohup .venv/bin/python -m bot.main >> logs/run.log 2>&1 &"
```

### Проверка
```bash
ssh bybit-bot "tail -50 /root/bybit_bot/logs/bot.log"
ssh bybit-bot "grep ERROR /root/bybit_bot/logs/bot.log | tail -20"
```

### SSH config (`~/.ssh/config`)
```
Host bybit-bot
    HostName 217.12.37.42
    User root
    Port 58291
    IdentityFile ~/.ssh/bybit_bot_ed25519
```

---

## 🔧 API интеграции

| API | Назначение | Эндпоинт |
|-----|------------|----------|
| Bybit REST (Demo) | Балансы, ордера | `https://api-demo.bybit.com/v5/*` |
| Bybit WS Public | Тикеры реалтайм | `wss://stream.bybit.com/v5/public/spot` |
| Bybit WS Private | Приватные данные | `wss://stream-demo.bybit.com/v5/private` |
| Perplexity | AI-анализ | `https://api.perplexity.ai/chat/completions` |
| Ollama | Локальная LLM | `http://localhost:11434/api/generate` |
| Telegram | Интерфейс | Bot API (aiogram 3.x) |

---

## 🔑 Переменные окружения (.env)

```env
# Bybit
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
BYBIT_BASE_URL=https://api-demo.bybit.com

# Perplexity
PERPLEXITY_API_KEY=...

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_ALLOWED_USER_IDS=6784568
```

---

## ⚠️ Известные проблемы и TODO

### Ошибки на сервере (актуальные)

1. **`reconcile_orphan_orders`** — метод вызывается в `controller.py`, но **не реализован** в `position_service.py`. Нужно добавить метод или убрать вызов.
2. **Demo Trading ограничения** — demo не поддерживает public WebSocket streams, поэтому используется mainnet для public WS (`get_pybit_ws_public_kwargs()`).
3. **Dust balances** — при продаже мелких остатков (AVAX, LINK) ошибка минимального размера ордера.

### TODO для доработки

- [ ] Реализовать `reconcile_orphan_orders()` в `PositionService`
- [ ] Добавить веб-дашборд (FastAPI + React)
- [ ] Улучшить AI промпт (добавить on-chain данные)
- [ ] Добавить поддержку mainnet (реальная торговля)
- [ ] Мониторинг здоровья бота (uptime, heartbeat)
- [ ] Бэкап БД (автоматический)

---

## ✅ История версий

### v2.0.0 (2026-02-09)
- ✅ Переход на Demo Trading (api-demo.bybit.com) вместо testnet
- ✅ `get_pybit_kwargs()` / `get_pybit_ws_public_kwargs()` — динамическая конфигурация
- ✅ Telegram переведён на aiogram 3.x (telegram_aiogram/)
- ✅ Полная документация PROJECT_STRUCTURE.md

### v1.9.0 (2026-01-06)
- ✅ DCA Balance Check — проверка USDT перед докупкой
- ✅ Improved Rejection Logging — `open_position` возвращает `(position, error_message)`
- ✅ Controller Logging — конкретная причина отказа

### v1.8.0 (2026-01-04)
- ✅ Advanced AI Prompt — Web Search + поле LOGIC
- ✅ Smart Scanner — фильтрация по доступности в среде

### v1.7.0 (2026-01-03)
- ✅ LLM Router (Perplexity / Ollama)
- ✅ Арбитраж: динамический сканер ликвидных пар
- ✅ Top Gainers: 24-часовая память

### v1.6.0 (2026-01-03)
- ✅ Спот-Фьючерс Арбитраж
- ✅ Telegram меню арбитража
- ✅ Funding updates: 0:00, 8:00, 16:00 UTC

### v1.5.0 (2026-01-02)
- ✅ ATR-based TP/SL (R/R 1:3)
- ✅ Тренд-фильтр (EMA20/EMA50)
- ✅ Position Sizing по AI Score

### v1.4.0 — v1.1.0
- Пирамидинг, Auto-SL, Breakeven, Time Exit, форматирование qty/price
