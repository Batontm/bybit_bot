"""
Клавиатуры для Telegram бота
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from config.trading_config import DEFAULT_TRADING_PAIRS


def get_main_menu_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Главное меню - ПОСТОЯННАЯ клавиатура внизу экрана.
    Не уплывает с сообщениями.
    """
    keyboard = [
        [
            KeyboardButton("📊 Анализ"),
            KeyboardButton("📈 Отчёты")
        ],
        [
            KeyboardButton("💼 Статус"),
            KeyboardButton("⚠️ Ошибки")
        ],
        [
            KeyboardButton("💹 Арбитраж"),
            KeyboardButton("⚙️ Настройки")
        ]
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,  # Уменьшить размер кнопок
        is_persistent=True     # Клавиатура всегда видна
    )


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Inline главное меню (для обратной совместимости)"""
    keyboard = [
        [
            InlineKeyboardButton("📊 Анализ", callback_data="menu_analysis"),
            InlineKeyboardButton("📈 Отчёты", callback_data="menu_reports")
        ],
        [
            InlineKeyboardButton("💼 Статус", callback_data="menu_status"),
            InlineKeyboardButton("⚠️ Ошибки", callback_data="menu_errors")
        ],
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_analysis_keyboard() -> InlineKeyboardMarkup:
    """Меню выбора пары для анализа"""
    keyboard = []
    
    # Кнопки по парам (по 2 в ряд)
    for i in range(0, len(DEFAULT_TRADING_PAIRS), 2):
        row = []
        for pair in DEFAULT_TRADING_PAIRS[i:i+2]:
            row.append(InlineKeyboardButton(
                pair,
                callback_data=f"analyze_{pair}"
            ))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="menu_main")])
    return InlineKeyboardMarkup(keyboard)


def get_analysis_result_keyboard(pair: str) -> InlineKeyboardMarkup:
    """Кнопки после анализа пары"""
    keyboard = [
        [InlineKeyboardButton("💰 Купить", callback_data=f"buy_{pair}")],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"analyze_{pair}")],
        [InlineKeyboardButton("◀️ К выбору пар", callback_data="menu_analysis")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_reports_keyboard() -> InlineKeyboardMarkup:
    """Меню отчётов"""
    keyboard = [
        [InlineKeyboardButton("💰 PnL по монетам (неделя)", callback_data="report_pnl_pairs")],
        [InlineKeyboardButton("📅 PnL по дням", callback_data="report_pnl_days")],
        [InlineKeyboardButton("🤖 ИИ-отчёт (неделя)", callback_data="report_llm_week")],
        [InlineKeyboardButton("🤖 ИИ-отчёт (месяц)", callback_data="report_llm_month")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_back_to_reports_keyboard() -> InlineKeyboardMarkup:
    """Кнопка назад к отчётам"""
    keyboard = [
        [InlineKeyboardButton("◀️ Назад", callback_data="menu_reports")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_status_keyboard() -> InlineKeyboardMarkup:
    """Кнопки для статуса"""
    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="status_refresh")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_errors_keyboard() -> InlineKeyboardMarkup:
    """Кнопки для ошибок"""
    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="menu_errors")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_settings_keyboard(is_paused: bool, auto_trading: bool, scanner_enabled: bool) -> InlineKeyboardMarkup:
    """Меню настроек"""
    pause_text = "▶️ Продолжить" if is_paused else "⏸️ Пауза"
    auto_text = "❌ Выкл авто-торговлю" if auto_trading else "✅ Вкл авто-торговлю"
    
    scanner_text = "🔄 Авто-поиск: ВКЛ" if scanner_enabled else "🔄 Авто-поиск: ВЫКЛ"
    
    keyboard = [
        [InlineKeyboardButton(pause_text, callback_data="settings_pause")],
        [InlineKeyboardButton(auto_text, callback_data="settings_auto_trade")],
        [InlineKeyboardButton(scanner_text, callback_data="settings_scanner")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_startup_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура при запуске"""
    keyboard = [
        [
            InlineKeyboardButton("📊 Анализ", callback_data="menu_analysis"),
            InlineKeyboardButton("📈 Отчёты", callback_data="menu_reports")
        ],
        [
            InlineKeyboardButton("💼 Статус", callback_data="menu_status"),
            InlineKeyboardButton("⚠️ Ошибки", callback_data="menu_errors")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_arbitrage_keyboard() -> InlineKeyboardMarkup:
    """Меню арбитража"""
    keyboard = [
        [InlineKeyboardButton("🔍 Сканер возможностей", callback_data="arb_scan")],
        [InlineKeyboardButton("📊 Открыть арбитраж", callback_data="arb_open")],
        [InlineKeyboardButton("❌ Закрыть все позиции", callback_data="arb_close_all")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="arb_refresh")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_arbitrage_pairs_keyboard() -> InlineKeyboardMarkup:
    """Выбор пары для арбитража"""
    pairs = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
    keyboard = []
    for pair in pairs:
        keyboard.append([InlineKeyboardButton(f"📈 {pair}", callback_data=f"arb_open_{pair}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="menu_arbitrage")])
    return InlineKeyboardMarkup(keyboard)
