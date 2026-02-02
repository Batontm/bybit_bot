"""
Обработчики команд и callback-ов для Telegram бота
"""
from telegram import Update
from telegram.ext import ContextTypes
from config.settings import logger
from ..services.analysis_service import analysis_service
from ..services.trading_service import trading_service
from ..services.stats_service import stats_service
from ..services.balance_service import balance_service
from ..services.arbitrage_service import arbitrage_service
from ..error_tracker import error_tracker
from . import keyboards, formatters


class TelegramHandlers:
    """Обработчики команд и callback-ов"""
    
    def __init__(self, controller=None):
        self.controller = controller
    
    # ========== КОМАНДЫ ==========
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        await self.show_main_menu(update)
    
    async def show_main_menu(self, update: Update):
        """Показать главное меню с ПОСТОЯННОЙ клавиатурой внизу"""
        # Получаем статус бота
        if self.controller:
            bot_mode = self.controller.get_mode()
            auto_trading = self.controller.is_auto_trading_enabled()
        else:
            bot_mode = "UNKNOWN"
            auto_trading = False
        
        text = formatters.format_main_menu(bot_mode, auto_trading)
        reply_keyboard = keyboards.get_main_menu_reply_keyboard()
        
        if update.callback_query:
            # При возврате из подменю - отправляем новое сообщение
            await update.callback_query.message.reply_text(
                text=text,
                reply_markup=reply_keyboard,
                parse_mode='HTML'
            )
        elif update.message:
            await update.message.reply_text(
                text=text,
                reply_markup=reply_keyboard,
                parse_mode='HTML'
            )
    
    # ========== ОБРАБОТЧИК ТЕКСТОВЫХ КНОПОК (ReplyKeyboard) ==========
    
    async def text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений от ReplyKeyboard"""
        text = update.message.text
        
        if text == "📊 Анализ":
            await self.show_analysis_menu_text(update)
        elif text == "📈 Отчёты":
            await self.show_reports_menu_text(update)
        elif text == "💼 Статус":
            await self.show_status_text(update)
        elif text == "⚠️ Ошибки":
            await self.show_errors_text(update)
        elif text == "💹 Арбитраж":
            await self.show_arbitrage_menu_text(update)
        elif text == "⚙️ Настройки":
            await self.show_settings_menu_text(update)
    
    # ========== ОБРАБОТЧИК КНОПОК ==========
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        # Главное меню
        if data == "menu_main":
            await self.show_main_menu(update)
        
        # Анализ
        elif data == "menu_analysis":
            await self.show_analysis_menu(query)
        elif data.startswith("analyze_"):
            pair = data.replace("analyze_", "")
            await self.analyze_pair(query, pair)
        
        # Отчёты
        elif data == "menu_reports":
            await self.show_reports_menu(query)
        elif data == "report_pnl_pairs":
            await self.show_pnl_by_pairs(query)
        elif data == "report_pnl_days":
            await self.show_pnl_by_days(query)
        elif data == "report_llm_week":
            await self.show_llm_report(query, "week")
        elif data == "report_llm_month":
            await self.show_llm_report(query, "month")
        
        # Статус
        elif data == "menu_status":
            await self.show_status(query)
        elif data == "status_refresh":
            await self.show_status(query)
        
        # Ошибки
        elif data == "menu_errors":
            await self.show_errors(query)
        
        # Настройки
        elif data == "menu_settings":
            await self.show_settings_menu(query)
        elif data == "settings_pause":
            await self.toggle_pause(query)
        elif data == "settings_auto_trade":
            await self.toggle_auto_trading(query)
        elif data == "settings_scanner":
            await self.toggle_scanner(query)
        
        # Ручная покупка
        elif data.startswith("buy_"):
            pair = data.replace("buy_", "")
            await self.manual_buy(query, pair)
        elif data.startswith("confirm_buy_"):
            pair = data.replace("confirm_buy_", "")
            await self.execute_buy(query, pair)
        
        # Арбитраж
        elif data == "menu_arbitrage":
            await self.show_arbitrage_menu(query)
        elif data == "arb_scan":
            await self.show_arbitrage_scan(query)
        elif data == "arb_open":
            await self.show_arbitrage_pairs(query)
        elif data.startswith("arb_open_"):
            pair = data.replace("arb_open_", "")
            await self.open_arbitrage_position(query, pair)
        elif data == "arb_close_all":
            await self.close_all_arbitrage(query)
        elif data == "arb_refresh":
            await self.show_arbitrage_menu(query)
    
    # ========== АНАЛИЗ ==========
    
    async def show_analysis_menu(self, query):
        """Меню выбора пары для анализа"""
        reply_markup = keyboards.get_analysis_keyboard()
        
        await query.edit_message_text(
            "📊 Выберите пару для анализа:",
            reply_markup=reply_markup
        )
    
    async def show_analysis_menu_text(self, update: Update):
        """Меню выбора пары для анализа (для текстовых кнопок)"""
        reply_markup = keyboards.get_analysis_keyboard()
        
        await update.message.reply_text(
            "📊 Выберите пару для анализа:",
            reply_markup=reply_markup
        )
    
    async def show_reports_menu_text(self, update: Update):
        """Меню отчётов (для текстовых кнопок)"""
        reply_markup = keyboards.get_reports_keyboard()
        
        await update.message.reply_text(
            "📈 Выберите тип отчёта:",
            reply_markup=reply_markup
        )
    
    async def show_status_text(self, update: Update):
        """Показать статус позиций (для текстовых кнопок)"""
        if self.controller:
            await self.controller.position_service.update_positions_prices()
        
        report = stats_service.get_positions_status_report()
        balances = balance_service.get_all_balances()
        text = report + formatters.format_status_balances(balances)
        
        if self.controller:
            fixed = self.controller.fixed_pairs
            dynamic = self.controller.dynamic_pairs
            
            text += "\n\n🎯 <b>Отслеживаемые пары:</b>\n"
            text += f"📌 Fix: {', '.join(fixed)}\n"
            if dynamic:
                text += f"🚀 Top: {', '.join(dynamic)}\n"
            else:
                text += "🚀 Top: (нет)"
        
        reply_markup = keyboards.get_status_keyboard()
        
        await update.message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    async def show_errors_text(self, update: Update):
        """Показать последние ошибки (для текстовых кнопок)"""
        errors = error_tracker.get_today_errors()
        text = formatters.format_errors(errors)
        reply_markup = keyboards.get_errors_keyboard()
        
        await update.message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    async def show_settings_menu_text(self, update: Update):
        """Меню настроек (для текстовых кнопок)"""
        if not self.controller:
            await update.message.reply_text("❌ Контроллер недоступен")
            return
        
        bot_mode = self.controller.get_mode()
        auto_trading = self.controller.is_auto_trading_enabled()
        scanner_enabled = self.controller.is_scanner_enabled()
        
        text = formatters.format_settings(bot_mode, auto_trading)
        reply_markup = keyboards.get_settings_keyboard(
            bot_mode == "PAUSED",
            auto_trading,
            scanner_enabled
        )
        
        await update.message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    async def analyze_pair(self, query, pair: str):
        """Провести анализ пары"""
        # Показываем индикатор загрузки
        await query.edit_message_text(f"🔄 Анализирую {pair} (это может занять время)...")
        
        # Запускаем анализ
        analysis = await analysis_service.analyze_pair(pair, timeframe="1h")
        
        text = formatters.format_analysis(analysis)
        reply_markup = keyboards.get_analysis_result_keyboard(pair)
        
        # Генерируем график
        chart_bytes = await analysis_service.get_analysis_chart(pair, analysis)
        
        # Если есть график - отправляем фото, удаляя "загрузку"
        if chart_bytes:
            # Сначала удаляем сообщение о загрузке
            await query.delete_message()
            
            # 1. Отправляем фото
            await query.message.reply_photo(
                photo=chart_bytes,
                caption=f"📉 График {pair}"
            )
            
            # 2. Отправляем детальный анализ отдельным сообщением
            await query.message.reply_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        else:
            # Иначе просто обновляем текст
            await query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
    
    # ========== ОТЧЁТЫ ==========
    
    async def show_reports_menu(self, query):
        """Меню отчётов"""
        reply_markup = keyboards.get_reports_keyboard()
        
        await query.edit_message_text(
            "📈 Выберите тип отчёта:",
            reply_markup=reply_markup
        )
    
    async def show_pnl_by_pairs(self, query):
        """Отчёт PnL по парам"""
        report = stats_service.get_pnl_by_pairs_report(days=7)
        reply_markup = keyboards.get_back_to_reports_keyboard()
        
        await query.edit_message_text(
            text=report,
            reply_markup=reply_markup
        )
    
    async def show_pnl_by_days(self, query):
        """Отчёт PnL по дням"""
        report = stats_service.get_pnl_by_days_report(days=7)
        reply_markup = keyboards.get_back_to_reports_keyboard()
        
        await query.edit_message_text(
            text=report,
            reply_markup=reply_markup
        )
    
    async def show_llm_report(self, query, period: str):
        """Отчёт по использованию Perplexity"""
        report = stats_service.get_llm_stats_report(period)
        reply_markup = keyboards.get_back_to_reports_keyboard()
        
        await query.edit_message_text(
            text=report,
            reply_markup=reply_markup
        )
    
    # ========== СТАТУС ==========
    
    async def show_status(self, query):
        """Показать статус позиций"""
        # Обновляем цены
        if self.controller:
            await self.controller.position_service.update_positions_prices()
        
        # Получаем отчёт
        report = stats_service.get_positions_status_report()
        
        # Добавляем балансы
        balances = balance_service.get_all_balances()
        text = report + formatters.format_status_balances(balances)
        
        # Добавляем информацию о парах
        if self.controller:
            fixed = self.controller.fixed_pairs
            dynamic = self.controller.dynamic_pairs
            
            text += "\n\n🎯 <b>Отслеживаемые пары:</b>\n"
            text += f"📌 Fix: {', '.join(fixed)}\n"
            if dynamic:
                text += f"🚀 Top: {', '.join(dynamic)}\n"
            else:
                text += "🚀 Top: (нет)"
        
        reply_markup = keyboards.get_status_keyboard()
        
        try:
            await query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            # Игнорируем ошибку, если текст не изменился
            if "Message is not modified" in str(e):
                pass
            else:
                logger.error(f"⚠️ Ошибка обновления статуса: {e}")
    
    # ========== ОШИБКИ ==========
    
    async def show_errors(self, query):
        """Показать последние ошибки"""
        errors = error_tracker.get_today_errors()
        text = formatters.format_errors(errors)
        reply_markup = keyboards.get_errors_keyboard()
        
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    # ========== НАСТРОЙКИ ==========
    
    async def show_settings_menu(self, query):
        """Меню настроек"""
        if not self.controller:
            await query.edit_message_text("❌ Контроллер недоступен")
            return
        
        bot_mode = self.controller.get_mode()
        auto_trading = self.controller.is_auto_trading_enabled()
        scanner_enabled = self.controller.is_scanner_enabled()
        
        text = formatters.format_settings(bot_mode, auto_trading)
        reply_markup = keyboards.get_settings_keyboard(
            bot_mode == "PAUSED",
            auto_trading,
            scanner_enabled
        )
        
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    async def toggle_pause(self, query):
        """Переключить паузу"""
        if not self.controller:
            await query.answer("❌ Контроллер недоступен")
            return
        
        current_mode = self.controller.get_mode()
        
        if current_mode == "PAUSED":
            self.controller.set_mode("ACTIVE")
            await query.answer("▶️ Бот возобновлён")
        else:
            self.controller.set_mode("PAUSED")
            await query.answer("⏸️ Бот поставлен на паузу")
        
        await self.show_settings_menu(query)
    
    async def toggle_auto_trading(self, query):
        """Переключить авто-торговлю"""
        if not self.controller:
            await query.answer("❌ Контроллер недоступен")
            return
        
        new_state = self.controller.toggle_auto_trading()
        
        if new_state:
            await query.answer("✅ Авто-торговля включена")
        else:
            await query.answer("❌ Авто-торговля выключена")
        
        await self.show_settings_menu(query)

    async def toggle_scanner(self, query):
        """Переключить сканер"""
        if not self.controller:
            await query.answer("❌ Контроллер недоступен")
            return
            
        new_state = self.controller.toggle_scanner()
        
        if new_state:
            await query.answer("✅ Сканер включен")
        else:
            await query.answer("❌ Сканер выключен")
            
        await self.show_settings_menu(query)
    
    # ========== РУЧНАЯ ПОКУПКА ==========
    
    async def manual_buy(self, query, pair: str):
        """Показать подтверждение покупки"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        # Получаем последний анализ
        analysis = await analysis_service.analyze_pair(pair, use_cache=True)
        
        if not analysis:
            await query.answer("❌ Сначала проведите анализ")
            return
        
        score = analysis.get('score', 0)
        signal = analysis.get('signal', 'WAIT')
        
        keyboard = [
            [InlineKeyboardButton("✅ Подтвердить покупку", callback_data=f"confirm_buy_{pair}")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"analyze_{pair}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"""⚠️ <b>Подтверждение покупки {pair}</b>

Score: {score}/100
Сигнал: {signal}

Вы уверены, что хотите открыть позицию?

• Размер: ~11 USDT (стандартный)
• TP: +2% от входа
• SL: -1% от входа""",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    
    async def execute_buy(self, query, pair: str):
        """Выполнить покупку"""
        await query.edit_message_text(f"⏳ Открываю позицию по {pair}...")
        
        try:
            # Получаем последний анализ для TP/SL
            analysis = await analysis_service.analyze_pair(pair, use_cache=True)
            
            if not analysis:
                analysis = {'score': 50, 'signal': 'WAIT'}
            
            # Открываем позицию
            position, open_error = await trading_service.open_position(pair, analysis)
            
            if position:
                await query.edit_message_text(
                    f"""✅ <b>Позиция открыта!</b>

Пара: {pair}
Цена входа: ${position.get('entry_price', 0):.4f}
Количество: {position.get('quantity', 0):.6f}
TP: ${position.get('tp_price', 0):.4f}
SL: ${position.get('sl_price', 0):.4f}""",
                    parse_mode='HTML'
                )
            else:
                await query.edit_message_text(
                    f"❌ Не удалось открыть позицию по {pair}\n\nОшибка: {open_error or 'неизвестно'}",
                    parse_mode='HTML'
                )
                
        except Exception as e:
            logger.error(f"❌ Ошибка ручной покупки {pair}: {e}")
            await query.edit_message_text(
                f"❌ Ошибка: {str(e)[:100]}",
                parse_mode='HTML'
            )
    
    # ========== АРБИТРАЖ ==========
    
    async def show_arbitrage_menu_text(self, update: Update):
        """Меню арбитража (для текстовых кнопок)"""
        dashboard = arbitrage_service.get_dashboard()
        positions = dashboard['positions']
        total_funding = dashboard['total_funding']
        apy = dashboard['apy']
        
        text = "<b>💹 СПОТ-ФЬЮЧЕРС АРБИТРАЖ</b>\n\n"
        
        if positions:
            text += f"📈 <b>Активные позиции:</b> {len(positions)}\n"
            for pos in positions:
                text += f"├─ {pos['pair']}: +${pos['accumulated_funding']:.4f}\n"
            text += f"\n💰 <b>Накопленный profit:</b> ${total_funding:.2f}\n"
            text += f"📊 <b>APY (годовых):</b> ~{apy:.1f}%\n"
        else:
            text += "📭 Нет открытых арбитражных позиций\n"
        
        await update.message.reply_text(
            text,
            parse_mode='HTML',
            reply_markup=keyboards.get_arbitrage_keyboard()
        )
    
    async def show_arbitrage_menu(self, query):
        """Меню арбитража (для inline кнопок)"""
        await query.answer()
        dashboard = arbitrage_service.get_dashboard()
        positions = dashboard['positions']
        total_funding = dashboard['total_funding']
        apy = dashboard['apy']
        
        text = "<b>💹 СПОТ-ФЬЮЧЕРС АРБИТРАЖ</b>\n\n"
        
        if positions:
            text += f"📈 <b>Активные позиции:</b> {len(positions)}\n"
            for pos in positions:
                text += f"├─ {pos['pair']}: +${pos['accumulated_funding']:.4f}\n"
            text += f"\n💰 <b>Накопленный profit:</b> ${total_funding:.2f}\n"
            text += f"📊 <b>APY (годовых):</b> ~{apy:.1f}%\n"
        else:
            text += "📭 Нет открытых арбитражных позиций\n"
        
        await query.edit_message_text(
            text,
            parse_mode='HTML',
            reply_markup=keyboards.get_arbitrage_keyboard()
        )
    
    async def show_arbitrage_scan(self, query):
        """Сканер арбитражных возможностей"""
        await query.answer("🔍 Сканирую...")
        
        opportunities = arbitrage_service.scan_funding_rates()
        
        text = "<b>🔍 СКАНЕР АРБИТРАЖА</b>\n\n"
        text += "<code>Пара      | Funding | APY    | Риск</code>\n"
        text += "<code>─────────────────────────────────</code>\n"
        
        for opp in opportunities:
            text += f"<code>{opp['pair']:<9} | {opp['funding_pct']:+.3f}% | {opp['apy']:+.1f}% | {opp['risk']}</code>\n"
        
        # Рекомендации
        good = [o['pair'] for o in opportunities if o['funding_rate'] >= 0.0001]
        if good:
            text += f"\n✅ <b>Рекомендуем:</b> {', '.join(good[:2])}"
        else:
            text += "\n⚠️ Нет выгодных возможностей сейчас"
        
        await query.edit_message_text(
            text,
            parse_mode='HTML',
            reply_markup=keyboards.get_arbitrage_keyboard()
        )
    
    async def show_arbitrage_pairs(self, query):
        """Показать выбор пар для открытия арбитража"""
        await query.answer()
        await query.edit_message_text(
            "<b>📊 Выберите пару для арбитража:</b>\n\n"
            "Будет куплено на СПОТ + открыт SHORT на фьючерсах",
            parse_mode='HTML',
            reply_markup=keyboards.get_arbitrage_pairs_keyboard()
        )
    
    async def open_arbitrage_position(self, query, pair: str):
        """Открыть арбитражную позицию"""
        await query.answer(f"🔄 Открываю {pair}...")
        
        # Размер позиции — фиксированный 100 USDT для тестов
        amount_usdt = 100
        
        success, message = await arbitrage_service.open_arbitrage(pair, amount_usdt)
        
        await query.edit_message_text(
            message,
            parse_mode='HTML',
            reply_markup=keyboards.get_arbitrage_keyboard()
        )
    
    async def close_all_arbitrage(self, query):
        """Закрыть все арбитражные позиции"""
        await query.answer("🔄 Закрываю...")
        
        closed, message = await arbitrage_service.close_all_arbitrages()
        
        await query.edit_message_text(
            message,
            parse_mode='HTML',
            reply_markup=keyboards.get_arbitrage_keyboard()
        )

