"""
Единый источник правды для промптов LLM.

Все провайдеры (Perplexity, Groq, DeepSeek, Ollama) используют
одинаковые формулировки и формат ответа, что упрощает A/B-тесты
и обеспечивает консистентный парсинг.
"""
from typing import Dict


SYSTEM_PROMPT_DEFAULT = (
    "Ты — опытный технический аналитик криптовалют. "
    "Специализация: скальпинг и интрадей-торговля на Bybit (SPOT). "
    "Главный приоритет — риск-менеджмент и защита капитала. "
    "Отвечай строго в указанном формате."
)

SYSTEM_PROMPT_WITH_WEB_SEARCH = (
    "Ты — ведущий аналитик криптовалют с доступом к данным в реальном времени. "
    "Твоя специализация — скальпинг и интрадей-торговля. "
    "Ты обязан выполнять поиск свежих новостей, но главный приоритет — защита капитала."
)


def build_analysis_prompt(
    pair: str,
    timeframe: str,
    market_data: Dict,
    *,
    with_web_search: bool = False,
) -> str:
    """Построить промпт для анализа пары.

    Args:
        pair: Торговая пара (например, "BTCUSDT").
        timeframe: Таймфрейм ("15m", "1h", "4h").
        market_data: Словарь с рыночными данными (price, change_24h, volume_24h, rsi, ...).
        with_web_search: Если True — добавить инструкцию использовать web search
            (имеет смысл только для Perplexity / других провайдеров с интернет-доступом).
    """
    tech_data = ""
    if market_data.get('rsi') is not None:
        tech_data = (
            f"\n- RSI(14): {market_data.get('rsi', 'N/A')}"
            f"\n- MACD: {market_data.get('macd_signal', 'N/A')}"
            f"\n- EMA тренд: {market_data.get('ema_trend', 'N/A')}"
        )

    web_search_block = ""
    if with_web_search:
        web_search_block = f"""

ДОПОЛНИТЕЛЬНО (web-search):
1. Найди новости за последние 6 часов по {pair}: листинги, партнёрства, упоминания в X/Twitter,
   аномальные всплески объёма, китовые транзакции.
2. Оцени сентимент: органический рост / памп-дамп / реакция на новости.
3. Если техника указывает на перегретость — игнорируй бычий новостной шум."""

    return f"""Проанализируй пару {pair} на таймфрейме {timeframe} для краткосрочной SPOT-сделки (long, профит 2-5%).

ДАННЫЕ:
- Цена: ${market_data.get('price', 'N/A')}
- Изменение 24ч: {market_data.get('change_24h', 'N/A')}%
- Объём 24ч: ${market_data.get('volume_24h', 'N/A')}{tech_data}{web_search_block}

ПРАВИЛА РИСК-МЕНЕДЖМЕНТА (СТРОГО):
- Главная цель — НЕ потерять деньги. Сомневаешься → WAIT.
- RSI(14) > 70 → SIGNAL=WAIT или AVOID, SCORE < 50.
- Рост цены за 24ч > +5% → избегай FOMO: SIGNAL=WAIT по умолчанию, SCORE ≤ 60.
- Объём аномально низкий → SIGNAL=AVOID.
- Объём растёт + RSI 50-65 + EMA восходящий → подтверждение, SCORE может быть выше.

КРИТЕРИИ SCORE (0-100):
- 80-100: Редко. Только сильный технический сетап без перегретости.
- 65-79: Умеренно. Техника в норме, риск контролируемый.
- 40-64: Wait. Слабый сигнал или повышенный риск.
- 0-39: Avoid. Перегретость / неопределённость.

ОТВЕТЬ СТРОГО В ФОРМАТЕ (без лишнего текста):
SCORE: [число]
SIGNAL: [BUY/WAIT/AVOID]
TARGET: [цена +2-5%]
STOP_LOSS: [цена -1.5% или ближайшая поддержка]
LOGIC: [2 предложения о технической причине решения]"""


def parse_analysis(content: str) -> Dict:
    """Парсинг ответа LLM в структурированный словарь.

    Формат ответа задан в `build_analysis_prompt()`.
    Парсер устойчив к дополнительному тексту/markdown вокруг.
    """
    lines = content.strip().split('\n')

    score = 50
    signal = "WAIT"
    target = 0.0
    stop_loss = 0.0
    summary = ""
    parsing_summary = False

    for line in lines:
        line_str = line.strip()
        # Снимаем markdown-обёртки: "**SCORE:** 45" → "SCORE: 45"
        line_str = line_str.lstrip('*_#- ').rstrip('*_ ')
        line_str = line_str.replace('**', '').replace('__', '')
        line_upper = line_str.upper()

        if line_upper.startswith('SCORE:'):
            try:
                score_str = line_str.split(':', 1)[1].strip()
                score = int(score_str.split('/')[0].strip())
            except Exception:
                pass

        elif line_upper.startswith('SIGNAL:'):
            signal_text = line_str.split(':', 1)[1].strip().upper()
            signal_text = signal_text.replace('*', '').replace('`', '')
            if any(x in signal_text for x in ['BUY', 'LONG']):
                signal = "BUY"
            elif any(x in signal_text for x in ['AVOID', 'SHORT', 'SELL']):
                signal = "AVOID"
            else:
                signal = "WAIT"

        elif line_upper.startswith('TARGET:'):
            try:
                val = line_str.split(':', 1)[1].strip()
                val = ''.join(c for c in val if c.isdigit() or c == '.')
                if val:
                    target = float(val)
            except Exception:
                pass

        elif line_upper.startswith('STOP_LOSS:') or line_upper.startswith('SL:'):
            try:
                val = line_str.split(':', 1)[1].strip()
                val = ''.join(c for c in val if c.isdigit() or c == '.')
                if val:
                    stop_loss = float(val)
            except Exception:
                pass

        elif line_upper.startswith('LOGIC:') or line_upper.startswith('РЕЗЮМЕ:'):
            parsing_summary = True
            if ':' in line_str:
                parts = line_str.split(':', 1)
                if len(parts) > 1 and parts[1].strip():
                    summary = parts[1].strip()

        elif parsing_summary:
            if any(line_upper.startswith(k) for k in
                   ['SCORE:', 'SIGNAL:', 'TARGET:', 'STOP_LOSS:', 'SL:']):
                parsing_summary = False
                continue
            if line_str:
                summary += " " + line_str

    if signal == "WAIT" and score < 40:
        signal = "AVOID"

    return {
        'score': score,
        'signal': signal,
        'target': target,
        'stop_loss': stop_loss,
        'summary': summary.strip() or "Анализ получен.",
    }
