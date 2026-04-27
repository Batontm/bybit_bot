"""
Клиент для работы с Groq API (OpenAI-совместимый).

Groq — быстрый бесплатный (free tier) провайдер LLM на LPU.
Free tier: ~30 req/min, 6000 tokens/min для llama-3.3-70b-versatile.
Документация: https://console.groq.com/docs/quickstart
"""
import httpx
import requests
from typing import Dict, Optional
from config.settings import logger
from config.api_config import GROQ_API_KEY, GROQ_BASE_URL, GROQ_MODEL
from config.trading_config import (
    MAX_LLM_REQUESTS_PER_DAY,
    MAX_LLM_COST_PER_MONTH,
)
from .db.llm_requests_repo import LLMRequestsRepository
from .error_tracker import error_tracker


class GroqClient:
    """Клиент для взаимодействия с Groq API (OpenAI-совместимый)."""

    def __init__(self):
        self.api_key = GROQ_API_KEY
        self.base_url = GROQ_BASE_URL
        self.model = GROQ_MODEL
        self.llm_repo = LLMRequestsRepository()
        self.is_available = bool(self.api_key)

        if not self.is_available:
            logger.warning("⚠️ Groq API key не установлен")

    def check_limits(self) -> tuple[bool, str]:
        """Проверить лимиты использования (используем общие LLM-лимиты)."""
        try:
            daily_stats = self.llm_repo.get_daily_stats()
            if daily_stats['total_requests'] >= MAX_LLM_REQUESTS_PER_DAY:
                return False, f"Достигнут дневной лимит запросов ({MAX_LLM_REQUESTS_PER_DAY})"
        except Exception:
            pass
        return True, ""

    async def analyze_pair(self, pair: str, timeframe: str,
                           market_data: Dict) -> Optional[Dict]:
        """Анализ торговой пары через Groq."""
        if not self.is_available:
            error_tracker.add_error("Groq", "ConfigError", "API ключ не установлен")
            return None

        can_use, reason = self.check_limits()
        if not can_use:
            logger.warning(f"⚠️ Groq недоступен: {reason}")
            return None

        prompt = self._build_analysis_prompt(pair, timeframe, market_data)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Ты — опытный технический аналитик криптовалют. "
                                    "Специализация: скальпинг и интрадей-торговля на Bybit (SPOT). "
                                    "Главный приоритет — риск-менеджмент и защита капитала. "
                                    "Отвечай строго в указанном формате."
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 600,
                        "temperature": 0.2,
                    },
                )

            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                error_tracker.add_error("Groq", "APIError", error_msg)
                logger.error(f"❌ Groq API ошибка: {error_msg}")

                try:
                    self.llm_repo.create_request({
                        'pair': pair,
                        'timeframe': timeframe,
                        'prompt_type': 'analysis',
                        'success': False,
                        'error_code': str(response.status_code),
                        'error_message': error_msg,
                    })
                except Exception:
                    pass
                return None

            result = response.json()
            content = result['choices'][0]['message']['content']
            analysis = self._parse_analysis(content)

            # Groq free tier — cost = 0
            cost_usd = 0.0

            try:
                self.llm_repo.create_request({
                    'pair': pair,
                    'timeframe': timeframe,
                    'prompt_type': 'analysis',
                    'score': analysis['score'],
                    'signal': analysis['signal'],
                    'summary': analysis['summary'],
                    'cost_usd': cost_usd,
                    'success': True,
                })
            except Exception:
                pass

            analysis['cost_usd'] = cost_usd
            analysis['provider'] = 'groq'

            logger.info(
                f"✅ Groq анализ: {pair} | Score: {analysis['score']} | "
                f"Signal: {analysis['signal']} | $0.00"
            )
            return analysis

        except httpx.TimeoutException:
            error_msg = "Timeout при запросе к Groq"
            error_tracker.add_error("Groq", "TimeoutError", error_msg)
            logger.error(f"⏱️ {error_msg}")
            return None

        except Exception as e:
            error_tracker.add_error("Groq", type(e).__name__, str(e))
            logger.error(f"❌ Ошибка Groq: {e}")
            return None

    def _build_analysis_prompt(self, pair: str, timeframe: str, market_data: Dict) -> str:
        """Построение промпта для технического анализа.

        Groq не имеет web-search, поэтому фокус на техническом анализе
        и риск-менеджменте. Новостной фон оценивается косвенно через
        объём и волатильность.
        """
        tech_data = ""
        if market_data.get('rsi') is not None:
            tech_data = f"""
- RSI(14): {market_data.get('rsi', 'N/A')}
- MACD: {market_data.get('macd_signal', 'N/A')}
- EMA тренд: {market_data.get('ema_trend', 'N/A')}"""

        prompt = f"""Проанализируй пару {pair} на таймфрейме {timeframe} для краткосрочной SPOT-сделки (long, профит 2-5%).

ДАННЫЕ:
- Цена: ${market_data.get('price', 'N/A')}
- Изменение 24ч: {market_data.get('change_24h', 'N/A')}%
- Объём 24ч: ${market_data.get('volume_24h', 'N/A')}{tech_data}

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

        return prompt

    def _parse_analysis(self, content: str) -> Dict:
        """Парсинг ответа от Groq (формат идентичен Perplexity)."""
        lines = content.strip().split('\n')

        score = 50
        signal = "WAIT"
        target = 0.0
        stop_loss = 0.0
        summary = ""

        parsing_summary = False

        for line in lines:
            line_str = line.strip()
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
                if any(line_upper.startswith(k) for k in ['SCORE:', 'SIGNAL:', 'TARGET:', 'STOP_LOSS:', 'SL:']):
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

    def test_connection(self) -> bool:
        """Тестовый запрос для проверки доступности Groq."""
        if not self.is_available:
            return False

        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 10,
                },
                timeout=10,
            )

            if response.status_code == 200:
                logger.info(f"✅ Groq API доступен (модель: {self.model})")
                return True
            else:
                logger.error(f"❌ Groq API недоступен: {response.status_code}")
                logger.error(f"   Ответ: {response.text[:200]}")
                return False

        except Exception as e:
            logger.error(f"❌ Ошибка проверки Groq: {e}")
            return False


# Глобальный экземпляр
groq_client = GroqClient()
