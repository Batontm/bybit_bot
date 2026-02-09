"""
Клиент для работы с Perplexity API
"""
import httpx
import requests
from typing import Dict, Optional
from datetime import datetime
from config.settings import logger, TIMEZONE
from config.api_config import PERPLEXITY_API_KEY, PERPLEXITY_BASE_URL, PERPLEXITY_MODEL
from config.trading_config import (
    MAX_LLM_REQUESTS_PER_DAY,
    MAX_LLM_COST_PER_MONTH,
    ESTIMATED_COST_PER_REQUEST
)
from .db.llm_requests_repo import LLMRequestsRepository
from .error_tracker import error_tracker


class PerplexityClient:
    """Клиент для взаимодействия с Perplexity API"""
    
    def __init__(self):
        self.api_key = PERPLEXITY_API_KEY
        self.base_url = PERPLEXITY_BASE_URL
        self.model = PERPLEXITY_MODEL
        self.llm_repo = LLMRequestsRepository()
        self.is_available = bool(self.api_key)
        
        if not self.is_available:
            logger.warning("⚠️ Perplexity API key не установлен")
    
    def check_limits(self) -> tuple[bool, str]:
        """Проверить лимиты использования"""
        daily_stats = self.llm_repo.get_daily_stats()
        if daily_stats['total_requests'] >= MAX_LLM_REQUESTS_PER_DAY:
            return False, f"Достигнут дневной лимит запросов ({MAX_LLM_REQUESTS_PER_DAY})"
        
        monthly_cost = self.llm_repo.get_monthly_cost()
        if monthly_cost >= MAX_LLM_COST_PER_MONTH:
            return False, f"Достигнут месячный бюджет (${MAX_LLM_COST_PER_MONTH})"
        
        if monthly_cost >= MAX_LLM_COST_PER_MONTH * 0.9:
            logger.warning(f"⚠️ Использовано {monthly_cost:.2f}$ из {MAX_LLM_COST_PER_MONTH}$")
        
        return True, ""
    
    async def analyze_pair(self, pair: str, timeframe: str, 
                          market_data: Dict) -> Optional[Dict]:
        """Анализ торговой пары через Perplexity"""
        if not self.is_available:
            error_tracker.add_error("Perplexity", "ConfigError", "API ключ не установлен")
            return None
        
        can_use, reason = self.check_limits()
        if not can_use:
            logger.warning(f"⚠️ Perplexity недоступен: {reason}")
            return None
        
        prompt = self._build_analysis_prompt(pair, timeframe, market_data)
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": "Ты — ведущий аналитик криптовалют с доступом к данным в реальном времени. Твоя специализация — скальпинг и интрадей торговля. Твоя цель: найти импульс (momentum) для сделки с профитом 2-5%."
                            },
                            {
                                "role": "user",
                                "content": prompt
                            }
                        ],
                        "max_tokens": 1000,
                        "temperature": 0.2
                    }
                )
            
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                error_tracker.add_error("Perplexity", "APIError", error_msg)
                logger.error(f"❌ Perplexity API ошибка: {error_msg}")
                
                self.llm_repo.create_request({
                    'pair': pair,
                    'timeframe': timeframe,
                    'prompt_type': 'analysis',
                    'success': False,
                    'error_code': str(response.status_code),
                    'error_message': error_msg
                })
                
                return None
            
            result = response.json()
            content = result['choices'][0]['message']['content']
            analysis = self._parse_analysis(content)
            
            usage = result.get('usage', {})
            cost_usd = self._calculate_cost(usage)
            
            self.llm_repo.create_request({
                'pair': pair,
                'timeframe': timeframe,
                'prompt_type': 'analysis',
                'score': analysis['score'],
                'signal': analysis['signal'],
                'summary': analysis['summary'], # В БД поле называется summary, но мы туда пишем LOGIC
                'cost_usd': cost_usd,
                'success': True
            })
            
            analysis['cost_usd'] = cost_usd
            
            logger.info(f"✅ Анализ получен: {pair} | Score: {analysis['score']} | ${cost_usd:.4f}")
            
            return analysis
            
        except httpx.TimeoutException:
            error_msg = "Timeout при запросе к Perplexity"
            error_tracker.add_error("Perplexity", "TimeoutError", error_msg)
            logger.error(f"⏱️ {error_msg}")
            return None
            
        except Exception as e:
            error_tracker.add_error("Perplexity", type(e).__name__, str(e))
            logger.error(f"❌ Ошибка Perplexity: {e}")
            return None
    
    def _build_analysis_prompt(self, pair: str, timeframe: str, market_data: Dict) -> str:
        """Построение промпта для анализа"""
        
        # Технические индикаторы если есть
        tech_data = ""
        if market_data.get('rsi'):
            tech_data = f"""
Технические индикаторы:
- RSI(14): {market_data.get('rsi', 'N/A')}
- MACD: {market_data.get('macd_signal', 'N/A')}
- EMA тренд: {market_data.get('ema_trend', 'N/A')}"""

        prompt = f"""Проанализируй торговую пару {pair} на таймфрейме {timeframe} для краткосрочной сделки.

ДАННЫЕ ИЗ ТЕРМИНАЛА:
- Цена: ${market_data.get('price', 'N/A')} (24h change: {market_data.get('change_24h', 'N/A')}%)
- Объем 24ч: ${market_data.get('volume_24h', 'N/A')}
- RSI(14): {market_data.get('rsi', 'N/A')} | MACD: {market_data.get('macd_signal', 'N/A')} | EMA Trend: {market_data.get('ema_trend', 'N/A')}

ТВОЯ ЗАДАЧА:
1. Выполни поиск (Web Search) актуальных новостей за последние 6 часов по тикеру {pair}. Ищи: листинги, партнерства, упоминания в X (Twitter), аномальные всплески объема или китовые транзакции.
2. Оцени рыночный сентимент: это органический рост, "памп и дамп" или реакция на новости?
3. Сопоставь технические индикаторы с внешним фоном, но приоритет — риск-менеджмент.

ПРАВИЛА РИСК-МЕНЕДЖМЕНТА (ОБЯЗАТЕЛЬНО):
- Ты — консервативный риск-менеджер. Главная цель — не потерять деньги.
- Если RSI(14) > 70 на 1h — ОБЯЗАТЕЛЬНО SIGNAL=WAIT или AVOID и SCORE < 50.
- Если рост цены за 24h > +5% — избегай FOMO: по умолчанию SIGNAL=WAIT, SCORE не выше 60, если нет очень сильных подтверждений.
- Игнорируй общий «бычий» новостной шум, если техника указывает на перекупленность.

КРИТЕРИИ SCORE (0-100):
- 80-100: Редко. Только если техника не перегрета и есть сильный подтвержденный драйвер.
- 65-79: Умеренно. Техника в норме, риски контролируемые.
- 40-64: Wait. Риск повышен или сигнал слабый.
- 0-39: Avoid. Перегретость/неопределенность/негатив.

ОТВЕТЬ СТРОГО В ФОРМАТЕ:
SCORE: [число]
SIGNAL: [BUY/WAIT/AVOID]
TARGET: [цена +2-5% в зависимости от волатильности]
STOP_LOSS: [цена -1.5% или уровень поддержки]
LOGIC: [Опиши в 2 предложениях: 1) Техническую причину. 2) Найденный новостной/сентимент фактор.]

ВАЖНО: Приоритет — качество входа и защита капитала. Если сомневаешься — выбирай WAIT."""
        
        return prompt
    
    def _parse_analysis(self, content: str) -> Dict:
        """Парсинг ответа от Perplexity"""
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
                except:
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
                    # Удаляем валюту и запятые
                    val = ''.join(c for c in val if c.isdigit() or c == '.')
                    if val:
                        target = float(val)
                except:
                    pass
                    
            elif line_upper.startswith('STOP_LOSS:') or line_upper.startswith('SL:'):
                try:
                    val = line_str.split(':', 1)[1].strip()
                    val = ''.join(c for c in val if c.isdigit() or c == '.')
                    if val:
                        stop_loss = float(val)
                except:
                    pass
            
            # Начало секции LOGIC
            elif line_upper.startswith('LOGIC:') or line_upper.startswith('РЕЗЮМЕ:') or 'LOGIC' in line_upper:
                parsing_summary = True
                if ':' in line_str:
                    parts = line_str.split(':', 1)
                    if len(parts) > 1 and parts[1].strip():
                        summary = parts[1].strip()
            
            # Сбор строк LOGIC
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
            'summary': summary.strip() or "Анализ получен, но резюме не распознано."
        }
    
    def _calculate_cost(self, usage: Dict) -> float:
        """Расчёт стоимости запроса"""
        if not usage:
            return ESTIMATED_COST_PER_REQUEST
        
        input_tokens = usage.get('prompt_tokens', 0)
        output_tokens = usage.get('completion_tokens', 0)
        
        input_cost = (input_tokens / 1000) * 0.001
        output_cost = (output_tokens / 1000) * 0.001
        
        return input_cost + output_cost
    
    def test_connection(self) -> bool:
        """Тестовый запрос для проверки API"""
        if not self.is_available:
            return False
        
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "user", "content": "Hello"}
                    ],
                    "max_tokens": 10
                },
                timeout=10
            )
            
            if response.status_code == 200:
                logger.info("✅ Perplexity API доступен")
                return True
            else:
                logger.error(f"❌ Perplexity API недоступен: {response.status_code}")
                logger.error(f"   Ответ: {response.text}")
                return False
            
        except Exception as e:
            logger.error(f"❌ Ошибка проверки Perplexity: {e}")
            return False


# Глобальный экземпляр
perplexity_client = PerplexityClient()
