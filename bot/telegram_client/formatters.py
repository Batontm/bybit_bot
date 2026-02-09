"""
Форматирование сообщений для Telegram
"""
from typing import Dict, List
from config.trading_config import (
    THRESHOLD_AUTO_TRADE,
    THRESHOLD_BUY,
    THRESHOLD_WAIT
)


def format_main_menu(bot_mode: str, auto_trading: bool) -> str:
    """Форматировать главное меню"""
    mode_emoji = {
        'ACTIVE': '🟢',
        'RISK_ONLY': '🟡',
        'PAUSED': '🔴'
    }
    
    return f"""🤖 <b>Bybit Trading Bot</b>

Режим: {mode_emoji.get(bot_mode, '⚪')} {bot_mode}
Авто-торговля: {'✅ Включена' if auto_trading else '❌ Выключена'}
Сеть: 🧪 TESTNET

Выберите действие:"""


def format_analysis(analysis: Dict) -> str:
    """Форматировать результат анализа"""
    if not analysis:
        return "❌ Анализ недоступен"
    
    pair = analysis.get('pair', 'N/A')
    score = analysis.get('score', 0)
    signal = analysis.get('signal', 'WAIT')
    summary = analysis.get('summary', 'Нет резюме')
    cached = analysis.get('cached', False)
    
    # Эмодзи для сигнала
    signal_emoji = {
        'BUY': '🟢',
        'WAIT': '🟡',
        'AVOID': '🔴'
    }
    
    # Рекомендация
    if score >= THRESHOLD_AUTO_TRADE:
        recommendation = "✅ Можно покупать (авто-вход разрешён)"
    elif score >= THRESHOLD_BUY:
        recommendation = "⚠️ Можно покупать (требуется подтверждение)"
    elif score >= THRESHOLD_WAIT:
        recommendation = "⏳ Лучше подождать"
    else:
        recommendation = "❌ Избегать сделки"
    
    cache_status = "📦 Из кэша" if cached else "🆕 Свежий анализ"
    
    # Технические индикаторы (если есть в market_data)
    tech_info = ""
    if analysis.get('rsi') or analysis.get('ema_trend') or analysis.get('macd_signal'):
        tech_info = f"""
📐 <b>Технические индикаторы:</b>
• RSI: {analysis.get('rsi', 'N/A')}
• EMA тренд: {analysis.get('ema_trend', 'N/A')}
• MACD: {analysis.get('macd_signal', 'N/A')}
"""
    
    return f"""📊 Анализ {pair}

{signal_emoji.get(signal, '⚪')} Сигнал: {signal}
⭐ Score: {score}/100

🎯 Цель: {analysis.get('target', 0) or 'Не указана'}
🛑 Stop-Loss: {analysis.get('stop_loss', 0) or 'Не указан'}
{tech_info}
📝 Резюме:
{summary}

{recommendation}

{cache_status}
"""


def format_settings(bot_mode: str, auto_trading: bool) -> str:
    """Форматировать меню настроек"""
    return f"""⚙️ <b>Настройки</b>

Режим: {bot_mode}
Авто-торговля: {'Включена' if auto_trading else 'Выключена'}"""


def format_errors(errors: List[Dict]) -> str:
    """Форматировать список ошибок"""
    if not errors:
        return "✅ Ошибок за сегодня не обнаружено"
    
    lines = [f"⚠️ <b>Ошибки за сегодня ({len(errors)}):</b>\n"]
    
    for err in errors[-10:]:  # Последние 10
        timestamp = err['timestamp'].split('T')[1][:5]  # HH:MM
        module = err['module']
        error_type = err['error_type']
        message = err['message'][:50]  # Первые 50 символов
        
        lines.append(f"🕐 {timestamp} | {module}")
        lines.append(f"   {error_type}: {message}")
        lines.append("")
    
    return "\n".join(lines)


def format_startup_message(startup_checks: dict) -> str:
    """Форматировать сообщение при запуске"""
    lines = ["🤖 <b>Бот запущен!</b>\n"]
    
    # Режим
    bot_mode = startup_checks.get('mode', 'UNKNOWN')
    lines.append(f"Режим: {bot_mode}")
    lines.append(f"Сеть: {'🧪 TESTNET' if startup_checks.get('testnet') else '🔴 MAINNET'}")
    lines.append("")
    
    # Проверки
    lines.append("<b>Проверки:</b>")
    
    checks = [
        ('bybit_rest', 'Bybit REST API'),
        ('bybit_ws', 'Bybit WebSocket'),
        ('perplexity', 'Perplexity API'),
        ('database', 'База данных')
    ]
    
    for key, name in checks:
        status = "✅" if startup_checks.get(key) else "❌"
        lines.append(f"{status} {name}")
    
    lines.append("")
    
    # Балансы
    balances = startup_checks.get('balances', [])
    if balances:
        lines.append("<b>Балансы:</b>")
        for bal in balances[:5]:
            lines.append(f"{bal['coin']}: {bal['total']:.6f}")
    
    return "\n".join(lines)


def format_status_balances(balances: List[Dict]) -> str:
    """Форматировать балансы для статуса"""
    lines = ["\n💼 <b>Балансы:</b>"]
    
    for bal in balances[:5]:  # Топ-5 балансов
        lines.append(f"{bal['coin']}: {bal['total']:.6f}")
    
    return "\n".join(lines)
