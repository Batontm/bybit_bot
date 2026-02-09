"""
Параметры риск-менеджмента и торговли
"""

# ============= РИСК-МЕНЕДЖМЕНТ =============

# Риски на сделку
MAX_RISK_PER_TRADE = 0.005      # 0.5% депозита на одну сделку
MAX_TOTAL_RISK = 0.05            # 5% суммарный риск по всем позициям

# Дневные лимиты
MAX_DAILY_LOSS = 0.03            # 3% депозита - дневной стоп
MAX_NEW_TRADES_PER_DAY = 15      # лимит новых входов за день
MAX_CONSECUTIVE_LOSSES = 3       # после 3 лоссов подряд - пауза

# Лимиты по позициям
MAX_ACTIVE_PAIRS = 3             # максимум 3 активные пары одновременно
MAX_OPEN_POSITIONS = 5           # максимум 5 открытых позиций

# ============= TP/SL ПО УМОЛЧАНИЮ =============

DEFAULT_TP_PERCENT = 0.02        # +2% от цены входа (fallback если ATR выключен)
DEFAULT_SL_PERCENT = 0.01        # -1% от цены входа (fallback если ATR выключен)

# ============= ATR-BASED TP/SL (адаптивные уровни) =============
ATR_BASED_TPSL_ENABLED = True     # Использовать ATR вместо фиксированных %
ATR_PERIOD = 14                   # Период ATR (количество свечей)
ATR_TIMEFRAME = "60"              # Таймфрейм: "60" = 1h, "240" = 4h
ATR_TP_MULTIPLIER = 3.0           # TP = entry + (ATR × 3.0) — R/R 1:3
ATR_SL_MULTIPLIER = 1.0           # SL = entry - (ATR × 1.0)

# ============= TREND FILTER (тренд-фильтр) =============
TREND_FILTER_ENABLED = True       # Не торговать против тренда
TREND_EMA_FAST = 20               # Быстрая EMA
TREND_EMA_SLOW = 50               # Медленная EMA
# Правило: открывать LONG только если EMA20 > EMA50 (бычий тренд)

# ============= PYRAMIDING (пирамидинг входа) =============
PYRAMIDING_ENABLED = True           # Включить пирамидинг
INITIAL_POSITION_PERCENT = 0.65     # Первоначальный вход = 65% от расчётного размера
PYRAMIDING_TRIGGER = 0.01         # Докупка при просадке -0.5% от входа
PYRAMIDING_ADD_PERCENT = 0.35       # Размер докупки = 35% (всего 100%)

# ============= SMART DCA (усреднение) =============
DCA_ENABLED = True                # Включить докупку после пирамидинга
DCA_TRIGGER_PERCENT = -0.015      # Докупка при просадке -1.5% (после пирамидинга)
DCA_MAX_ENTRIES = 2               # Максимум 2 докупки на позицию
DCA_MIN_SCORE = 50                # Минимальный AI score для докупки
DCA_POSITION_MULTIPLIER = 0.5     # Размер докупки = 50% от позиции

# ============= BREAKEVEN (безубыток) =============
BREAKEVEN_ENABLED = True          # Включить перенос SL в безубыток
BREAKEVEN_TRIGGER_PERCENT = 0.01  # При +1% переносим SL в точку входа
BREAKEVEN_BUFFER = 0.001          # Буфер +0.1% над ценой входа

# ============= TRAILING STOP (улучшенный) =============
TRAILING_ACTIVATION = 0.015       # Активация: +3% прибыли (было +1%)
TRAILING_STEP = 0.015            # Держать SL на 1.5% от текущей цены (было 1%)

# ============= TIME EXIT (выход по времени) =============
TIME_EXIT_ENABLED = True          # Включить автозакрытие по времени
MAX_POSITION_HOURS = 16            # Закрывать позиции старше 8 часов (было 4)
STALE_MOVE_THRESHOLD = 0.0      # Позиция "мёртвая" если движение < 0.5%

# ============= ПАРАМЕТРЫ АНАЛИЗА =============

# Пороги для сигналов
THRESHOLD_AUTO_TRADE = 80        # авто-вход только при score >= 80 (было 80)
THRESHOLD_BUY = 70               # сигнал BUY при score >= 70 (было 70)
THRESHOLD_WAIT = 50              # сигнал WAIT при 50 <= score < 70 (было 50)
# score < 40 = AVOID

# ============= PERPLEXITY ЛИМИТЫ =============

# Бюджет: 30$ в месяц
MAX_LLM_COST_PER_MONTH = 30.0    # $ в месяц
MAX_LLM_REQUESTS_PER_DAY = 100    # запросов в день

# Интервал между анализами одной пары
MIN_ANALYSIS_INTERVAL_PER_PAIR = 300  # секунд (5 минут)

# Средняя стоимость запроса (для оценки)
ESTIMATED_COST_PER_REQUEST = 0.02     # $

# ============= LLM PROVIDER MODE =============
# Режимы: PERPLEXITY_ONLY, LOCAL_ONLY, HYBRID, PREFER_LOCAL
LLM_PROVIDER_MODE = "PERPLEXITY_ONLY"  # Ollama отключён (недостаточно RAM)

# ============= ТОРГОВЫЕ ПАРЫ ПО УМОЛЧАНИЮ =============

DEFAULT_TRADING_PAIRS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
]

# Таймфреймы для анализа
ANALYSIS_TIMEFRAMES = ["1h", "4h"]

# ============= ПАРАМЕТРЫ ОРДЕРОВ =============

# Минимальные размеры позиций (для безопасности)
MIN_ORDER_SIZE_USD = 5.0         # минимум 5 USDT на ордер

# Проскальзывание
MAX_SLIPPAGE_PERCENT = 0.002     # 0.2% максимальное проскальзывание

# Таймауты
ORDER_TIMEOUT_SECONDS = 30       # таймаут на исполнение ордера

# ============= РАСПИСАНИЕ =============

# Интервал проверки сигналов (секунды)
SIGNAL_CHECK_INTERVAL = 600      # 10 минут

# Интервал обновления балансов
BALANCE_UPDATE_INTERVAL = 60     # 1 минута

# Интервал проверки TP/SL
TPSL_CHECK_INTERVAL = 10         # 10 секунд

# ============= ERROR TRACKER =============

ERROR_BUFFER_SIZE = 200          # размер кольцевого буфера ошибок

# ============= AUTO ARBITRAGE (спот-фьючерс) =============
ARBITRAGE_ENABLED = True         # Включить авто-арбитраж
ARBITRAGE_CHECK_INTERVAL = 3600  # Проверка возможностей каждый час (секунды)
ARBITRAGE_MIN_FUNDING_RATE = 0.0001  # Минимальный funding rate (0.01%)
ARBITRAGE_MAX_POSITIONS = 3      # Максимум арбитражных позиций
ARBITRAGE_POSITION_SIZE_USD = 100  # Размер позиции в USDT
ARBITRAGE_FUNDING_UPDATE_HOURS = 8  # Обновление funding каждые 8 часов
ARBITRAGE_MIN_VOLUME_USDT = 10_000_000  # Мин. объем торгов за 24ч для пары
ARBITRAGE_SCAN_LIMIT = 50           # Сколько топ-ликвидных пар сканировать

# Усиление авто-арбитража в «красном» режиме (когда новые сделки запрещены Светофором)
# Идея: деньги не простаивают, а работают в market-neutral funding arbitrage.
ARBITRAGE_RED_MODE_ENABLED = True
ARBITRAGE_RED_MAX_POSITIONS = 8
# Максимальная доля стейбл-депозита (USDT+USDC), которая может быть задействована в арбитраже
ARBITRAGE_RED_TOTAL_ALLOCATION_PCT = 0.30
# Минимальный размер одной арбитражной позиции (защита от микросделок)
ARBITRAGE_RED_MIN_POSITION_SIZE_USD = 50


