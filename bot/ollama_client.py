"""
Клиент для работы с Ollama (локальная LLM)
"""
import httpx
from typing import Dict, Optional
from config.settings import logger
from config.api_config import OLLAMA_HOST, OLLAMA_MODEL
from .error_tracker import error_tracker


class OllamaClient:
    """Клиент для взаимодействия с Ollama API (локальная LLM)"""
    
    def __init__(self):
        self.host = OLLAMA_HOST
        self.model = OLLAMA_MODEL
        self.is_available = False
        self._check_availability()
    
    def _check_availability(self) -> None:
        """Проверить доступность Ollama"""
        try:
            import requests
            response = requests.get(f"{self.host}/api/tags", timeout=5)
            if response.status_code == 200:
                self.is_available = True
                logger.info(f"✅ Ollama доступен: {self.host} (модель: {self.model})")
            else:
                logger.warning(f"⚠️ Ollama недоступен: HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"⚠️ Ollama недоступен: {e}")
            self.is_available = False
    
    async def analyze_pair(self, pair: str, timeframe: str, 
                          market_data: Dict) -> Optional[Dict]:
        """Анализ торговой пары через Ollama (локальная LLM)"""
        if not self.is_available:
            error_tracker.add_error("Ollama", "ConfigError", "Ollama недоступен")
            return None
        
        prompt = self._build_analysis_prompt(pair, timeframe, market_data)
        
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.host}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": 0.3,
                            "num_predict": 500
                        }
                    }
                )
            
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                error_tracker.add_error("Ollama", "APIError", error_msg)
                logger.error(f"❌ Ollama API ошибка: {error_msg}")
                return None
            
            result = response.json()
            content = result.get('response', '')
            analysis = self._parse_analysis(content)
            
            # Ollama бесплатный — cost = 0
            analysis['cost_usd'] = 0.0
            analysis['provider'] = 'ollama'
            
            logger.info(f"✅ Ollama анализ: {pair} | Score: {analysis['score']} | $0.00")
            
            return analysis
            
        except httpx.TimeoutException:
            error_msg = "Timeout при запросе к Ollama"
            error_tracker.add_error("Ollama", "TimeoutError", error_msg)
            logger.error(f"⏱️ {error_msg}")
            return None
            
        except Exception as e:
            error_tracker.add_error("Ollama", type(e).__name__, str(e))
            logger.error(f"❌ Ошибка Ollama: {e}")
            return None
    
    def _build_analysis_prompt(self, pair: str, timeframe: str, market_data: Dict) -> str:
        """Построение промпта для анализа"""
        
        tech_data = ""
        if market_data.get('rsi'):
            tech_data = f"""
Технические индикаторы:
- RSI(14): {market_data.get('rsi', 'N/A')}
- MACD: {market_data.get('macd_signal', 'N/A')}
- EMA тренд: {market_data.get('ema_trend', 'N/A')}"""

        prompt = f"""Ты эксперт-трейдер криптовалют. Проанализируй {pair} для краткосрочной SPOT сделки ({timeframe}).

ТЕКУЩИЕ ДАННЫЕ:
- Цена: ${market_data.get('price', 'N/A')}
- Изменение 24ч: {market_data.get('change_24h', 'N/A')}%
- Объём 24ч: ${market_data.get('volume_24h', 'N/A')}{tech_data}

ЗАДАЧА: Оцени вероятность роста цены на 2-5% в ближайшие 1-4 часа.

ОТВЕТ СТРОГО В ФОРМАТЕ:
SCORE: [0-100]
SIGNAL: [BUY/WAIT/AVOID]
РЕЗЮМЕ: [2-3 предложения]"""
        
        return prompt
    
    def _parse_analysis(self, content: str) -> Dict:
        """Парсинг ответа от Ollama"""
        lines = content.strip().split('\n')
        
        score = 50
        signal = "WAIT"
        summary = ""
        
        parsing_summary = False
        
        for line in lines:
            line_str = line.strip()
            line_upper = line_str.upper()
            
            if line_upper.startswith('SCORE:'):
                try:
                    score_str = line_str.split(':', 1)[1].strip()
                    # Обработка форматов: "75", "75/100", "75%"
                    score_str = score_str.replace('%', '').split('/')[0].strip()
                    score = int(float(score_str))
                    score = max(0, min(100, score))  # Ограничиваем 0-100
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
            
            elif line_upper.startswith('РЕЗЮМЕ:') or line_upper.startswith('SUMMARY:'):
                parsing_summary = True
                if ':' in line_str:
                    parts = line_str.split(':', 1)
                    if len(parts) > 1 and parts[1].strip():
                        summary = parts[1].strip()
            
            elif parsing_summary:
                if any(line_upper.startswith(k) for k in ['SCORE:', 'SIGNAL:']):
                    parsing_summary = False
                    continue
                if line_str:
                    summary += " " + line_str
        
        # Авто-корректировка signal на основе score
        if signal == "WAIT":
            if score >= 70:
                signal = "BUY"
            elif score < 50:
                signal = "AVOID"
        
        return {
            'score': score,
            'signal': signal,
            'target': 0.0,
            'stop_loss': 0.0,
            'summary': summary.strip() or "Анализ получен от локальной LLM."
        }
    
    def test_connection(self) -> bool:
        """Тестовый запрос для проверки Ollama"""
        try:
            import requests
            response = requests.post(
                f"{self.host}/api/generate",
                json={
                    "model": self.model,
                    "prompt": "Say hello",
                    "stream": False,
                    "options": {"num_predict": 10}
                },
                timeout=30
            )
            
            if response.status_code == 200:
                logger.info("✅ Ollama API доступен")
                self.is_available = True
                return True
            else:
                logger.error(f"❌ Ollama API недоступен: {response.status_code}")
                return False
            
        except Exception as e:
            logger.error(f"❌ Ошибка проверки Ollama: {e}")
            return False


# Глобальный экземпляр
ollama_client = OllamaClient()
