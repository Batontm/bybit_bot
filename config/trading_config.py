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
MAX_CONSECUTIVE_LOSSES = 3       # после 3 убыточных сделок подряд — пауза новых входов
LOSS_STREAK_COOLDOWN_HOURS = 24   # ...на 24 часа (сопровождение открытых позиций продолжается)
MAX_DRAWDOWN_STOP = 0.10          # просадка капитала 10% от пика — новые входы останавливаются до ручного возобновления
VOL_RISK_SCALING = True           # при панике на рынке (ATR BTC 4H выше среднего) риск сделки уменьшается
VOL_RISK_MIN_FACTOR = 0.5         # ...но не меньше чем вдвое

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
# ВЫКЛЮЧЕН по умолчанию: новая воронка входа сразу берёт полный размер, рассчитанный
# от стопа за минимумом отката (риск = MAX_RISK_PER_TRADE). Докупка в просадку при
# том же стопе увеличивает риск сверх лимита. До v2.2 пирамидинг/DCA фактически не
# работали (отсутствовал метод get_add_action), так что поведение бота не меняется.
PYRAMIDING_ENABLED = False          # Включить пирамидинг
INITIAL_POSITION_PERCENT = 0.65     # Первоначальный вход = 65% от расчётного размера
PYRAMIDING_TRIGGER = -0.005         # Докупка при просадке -0.5% от входа (ОТРИЦАТЕЛЬНОЕ число!)
PYRAMIDING_ADD_PERCENT = 0.35       # Размер докупки = 35% от расчётного (всего 100%)

# ============= SMART DCA (усреднение) =============
DCA_ENABLED = False               # Включить докупку после пирамидинга (см. комментарий выше)
DCA_TRIGGER_PERCENT = -0.015      # Докупка при просадке -1.5% (после пирамидинга)
DCA_MAX_ENTRIES = 2               # Максимум 2 докупки на позицию
DCA_MIN_SCORE = 50                # Минимальный технический балл для докупки
DCA_POSITION_MULTIPLIER = 0.5     # Размер докупки = 50% от позиции

# ============= КОМИССИИ =============
TRADING_FEE_RATE = 0.001          # спот Bybit без VIP: 0.10% и для maker, и для taker
SLIPPAGE_ESTIMATE = 0.0005        # оценка проскальзывания на сделку (0.05%)

# ============= ВЫХОДЫ (всё в R, R = вход − начальный стоп) =============
# Безубыток: при +1R стоп переносится на вход + запас, покрывающий комиссии
# полного круга (0.1% × 2 + проскальзывание). Раньше запас был 0.1% — меньше
# комиссий, и «безубыточные» закрытия на деле были в минус.
BREAKEVEN_ENABLED = True
EXIT_BREAKEVEN_R = 1.0
EXIT_BREAKEVEN_FEE_BUFFER = 0.0025

# Частичная фиксация: половина позиции — лимитным ордером на +1.5R,
# остаток — на тейк FUNNEL_TP_R; после первой части стоп в безубыток.
EXIT_PARTIAL_ENABLED = True
EXIT_PARTIAL_R = 1.5
EXIT_PARTIAL_FRACTION = 0.5

# Трейлинг остатка после частичной фиксации: стоп = цена − 2×ATR(1h) (только вверх)
TRAILING_ENABLED = True
EXIT_TRAIL_ATR_MULT = 2.0
EXIT_TRAIL_MIN_STEP = 0.002       # двигаем стоп, если он поднимается хотя бы на 0.2%

# Выход по времени: позиция старше N часов, не дошедшая до +0.5R, закрывается
TIME_EXIT_ENABLED = True
EXIT_TIME_HOURS = 16
EXIT_TIME_MIN_R = 0.5

# ============= ПАРАМЕТРЫ АНАЛИЗА =============

# Пороги технического балла pre-filter (стратегия LEGACY и докупки)
THRESHOLD_AUTO_TRADE = 70        # авто-вход только при score >= 70 (стратегия LEGACY)
THRESHOLD_BUY = 60               # сигнал BUY при score >= 60
THRESHOLD_WAIT = 50              # сигнал WAIT при 50 <= score < 60
# score < 40 = AVOID

# ============= СТРАТЕГИЯ ВХОДА =============
# "FUNNEL" — воронка: отбор ликвидных пар → тренд 4h → откат 1h → триггер 15m →
#            поток ордеров / OI / funding → балльная оценка →
#            лимитный post-only вход, стоп за минимумом отката.
# "LEGACY" — старая логика: pre-filter (RSI/EMA/MACD/объём 1h) → тех. вето → рыночный вход.
# ИИ (Perplexity / DeepSeek / Groq / Ollama) из бота удалён в v2.3.
ENTRY_STRATEGY = "FUNNEL"

# --- Шаг 1. Отбор пар (get_tickers spot + linear) ---
FUNNEL_MIN_TURNOVER_USDT = 5_000_000   # мин. оборот спота за 24ч
FUNNEL_MAX_SPREAD = 0.001              # макс. спред bid/ask (0.1%)
FUNNEL_CHANGE_24H_MIN = 0.0            # % за 24ч: монета в движении...
FUNNEL_CHANGE_24H_MAX = 8.0            # ...но ещё не на пике
FUNNEL_REQUIRE_PERP = True             # нужен бессрочный фьючерс (данные OI/funding, отсев мусора)
FUNNEL_UNIVERSE_SIZE = 25              # сколько пар (по обороту) держать в отборе
FUNNEL_EXCLUDED_BASES = [              # стейблы, обёртки и т.п.
    'USDC', 'DAI', 'FDUSD', 'USDE', 'EUR', 'BUSD', 'USDD', 'TUSD', 'PYUSD',
    'USDV', 'GUSD', 'ZUSD', 'USDR', 'WBTC', 'STETH', 'WETH', 'XAUT', 'PAXG',
]

# --- Шаг 2. Сетап на старших ТФ (только закрытые свечи) ---
FUNNEL_TREND_EMA = 50                  # 4h: цена выше EMA50 и EMA50 растёт
FUNNEL_TREND_SLOPE_BARS = 6            # наклон EMA50 4h считаем за 6 свечей (сутки)
FUNNEL_PULLBACK_RSI_MIN = 38           # 1h: RSI в зоне паузы тренда
FUNNEL_PULLBACK_RSI_MAX = 58
FUNNEL_PULLBACK_ZONE_ATR = 0.5         # минимум последних свечей в пределах 0.5×ATR от EMA20/EMA50 1h
FUNNEL_PULLBACK_BARS = 6               # смотрим откат за последние 6 часов
FUNNEL_RS_DAYS = 7                     # сила к BTC за 7 дней (по 4h свечам)

# --- Шаг 3. Триггер на 15m ---
FUNNEL_TRIGGER_TF = "15"
FUNNEL_TRIGGER_LOOKBACK = 2            # триггер должен быть в последних 2 закрытых свечах
FUNNEL_TRIGGER_VOLUME_MULT = 1.5       # всплеск объёма: > 1.5× среднего за 20 свечей
FUNNEL_MAX_CHASE = 0.01                # не входить, если цена ушла > 1% от закрытия свечи-триггера

# --- Шаг 4. Поток ордеров и деривативы ---
FUNNEL_BOOK_DEPTH_PCT = 0.01           # глубина стакана ±1% от цены
FUNNEL_BOOK_IMBALANCE_GOOD = 1.3       # биды/аски > 1.3 — плюс
FUNNEL_ASK_WALL_MULT = 5.0             # уровень аска > 5× медианного уровня в пределах 0.5% — «стена»
FUNNEL_AGGRESSOR_WINDOW_MIN = 15       # окно для доли агрессивных покупок
FUNNEL_AGGRESSOR_GOOD = 0.55           # > 55% объёма — рыночные покупки
FUNNEL_OI_HOURS = 4                    # изменение OI и цены за 4 часа
FUNNEL_FUNDING_GOOD_MAX = 0.0003       # funding ≤ 0.03% — норма
FUNNEL_FUNDING_VETO = 0.0005           # funding > 0.05% — лонги перегреты, вето
FUNNEL_LONG_RATIO_CROWDED = 0.70       # > 70% аккаунтов в лонге — толпа, минус

# --- Шаг 5. Оценка, вето, исполнение ---
FUNNEL_MIN_SCORE = 70                  # минимальный балл для входа (из 100)
FUNNEL_STOP_BUFFER_ATR = 0.1           # стоп = минимум отката − 0.1×ATR(1h)
FUNNEL_STOP_MIN_ATR = 0.5              # стоп не ближе 0.5×ATR(1h) (шум)
FUNNEL_STOP_MAX_ATR = 2.5              # стоп дальше 2.5×ATR(1h) — сделку пропускаем
FUNNEL_TP_R = 2.5                      # тейк (вторая половина позиции) = вход + 2.5R
# Фильтр комиссий. Издержки полного круга c = вход × (2×0.1% + 0.05%) ≈ 0.25% цены.
# Чистый R/R = (тейк − вход − c) / (вход − стоп + c). При тейке 2.5R порог 1.8
# и доля комиссий ≤ 25% риска означают: стоп не ближе ~1% от цены.
FUNNEL_MIN_NET_RR = 1.8
FUNNEL_MAX_FEE_SHARE = 0.25
FUNNEL_LIMIT_TIMEOUT_SEC = 90          # сколько ждать исполнения post-only ордера
FUNNEL_CORR_MAX = 0.8                  # корреляция 1h доходностей за 7 дней с открытой позицией
FUNNEL_MAX_DEEP_CHECKS = 10            # макс. пар на глубокую проверку (стакан/OI) за цикл
FUNNEL_LOG_SIGNALS = True              # писать баллы в таблицу entry_signals

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
ARBITRAGE_MIN_FUNDING_RATE = 0.0001  # Минимальный funding rate (0.01%) — нижняя граница
ARBITRAGE_MAX_POSITIONS = 2      # Максимум арбитражных позиций (whitelist — только BTC и ETH)
# Комиссии полного круга: спот купить+продать (taker 0.1% × 2) + перп открыть+закрыть (taker 0.055% × 2)
ARBITRAGE_FEE_ROUNDTRIP = 0.0031
# Вход, только если funding окупает комиссии не дольше чем за N дней (3 начисления в сутки).
# При 3 днях нужен funding ≥ 0.0031 / 9 ≈ 0.034% за 8 часов.
ARBITRAGE_MAX_BREAKEVEN_DAYS = 3.0
# Выход: если N последних начисленных funding подряд отрицательные (шорт платит)
ARBITRAGE_EXIT_NEGATIVE_PERIODS = 2
ARBITRAGE_POSITION_SIZE_USD = 100  # Размер позиции в USDT
ARBITRAGE_FUNDING_UPDATE_HOURS = 8  # Обновление funding каждые 8 часов
ARBITRAGE_MIN_VOLUME_USDT = 10_000_000  # Мин. объем торгов за 24ч для пары
ARBITRAGE_SCAN_LIMIT = 50           # Сколько топ-ликвидных пар сканировать

# Усиление авто-арбитража в «красном» режиме (когда новые сделки запрещены Светофором)
# Идея: деньги не простаивают, а работают в market-neutral funding arbitrage.
ARBITRAGE_RED_MODE_ENABLED = True
ARBITRAGE_RED_MAX_POSITIONS = 2  # whitelist — только BTC и ETH, больше 2 позиций не бывает
# Максимальная доля стейбл-депозита (USDT+USDC), которая может быть задействована в арбитраже
ARBITRAGE_RED_TOTAL_ALLOCATION_PCT = 0.30
# Минимальный размер одной арбитражной позиции (защита от микросделок)
ARBITRAGE_RED_MIN_POSITION_SIZE_USD = 50


