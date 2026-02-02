"""
LLM Router - маршрутизатор между AI провайдерами (Perplexity / Ollama)
"""
from typing import Dict, Optional
from config.settings import logger
from config.trading_config import LLM_PROVIDER_MODE
from .perplexity_client import perplexity_client
from .ollama_client import ollama_client
from .error_tracker import error_tracker


class LLMRouter:
    """Маршрутизатор запросов между LLM провайдерами"""
    
    # Режимы работы
    PERPLEXITY_ONLY = "PERPLEXITY_ONLY"
    LOCAL_ONLY = "LOCAL_ONLY"
    HYBRID = "HYBRID"          # Perplexity + Local fallback
    PREFER_LOCAL = "PREFER_LOCAL"  # Local + Perplexity fallback
    
    def __init__(self):
        self.mode = LLM_PROVIDER_MODE
        self.perplexity = perplexity_client
        self.ollama = ollama_client
        
        logger.info(f"🤖 LLM Router: режим {self.mode}")
        logger.info(f"   Perplexity: {'✅' if self.perplexity.is_available else '❌'}")
        logger.info(f"   Ollama: {'✅' if self.ollama.is_available else '❌'}")
    
    async def analyze_pair(self, pair: str, timeframe: str, 
                          market_data: Dict) -> Optional[Dict]:
        """
        Анализ пары через выбранного провайдера.
        
        Режимы:
        - PERPLEXITY_ONLY: только Perplexity
        - LOCAL_ONLY: только Ollama
        - HYBRID: Perplexity, fallback на Ollama
        - PREFER_LOCAL: Ollama, fallback на Perplexity
        """
        
        if self.mode == self.PERPLEXITY_ONLY:
            return await self._call_perplexity(pair, timeframe, market_data)
        
        elif self.mode == self.LOCAL_ONLY:
            return await self._call_ollama(pair, timeframe, market_data)
        
        elif self.mode == self.HYBRID:
            # Сначала Perplexity, fallback на Ollama
            result = await self._call_perplexity(pair, timeframe, market_data)
            if result is None:
                logger.info("🔄 Fallback на Ollama...")
                result = await self._call_ollama(pair, timeframe, market_data)
            return result
        
        elif self.mode == self.PREFER_LOCAL:
            # Сначала Ollama, fallback на Perplexity
            result = await self._call_ollama(pair, timeframe, market_data)
            if result is None:
                logger.info("🔄 Fallback на Perplexity...")
                result = await self._call_perplexity(pair, timeframe, market_data)
            return result
        
        else:
            logger.error(f"❌ Неизвестный режим LLM: {self.mode}")
            return None
    
    async def _call_perplexity(self, pair: str, timeframe: str, 
                               market_data: Dict) -> Optional[Dict]:
        """Вызов Perplexity API"""
        if not self.perplexity.is_available:
            logger.debug("Perplexity недоступен")
            return None
        
        result = await self.perplexity.analyze_pair(pair, timeframe, market_data)
        if result:
            result['provider'] = 'perplexity'
        return result
    
    async def _call_ollama(self, pair: str, timeframe: str, 
                          market_data: Dict) -> Optional[Dict]:
        """Вызов Ollama (локальная LLM)"""
        if not self.ollama.is_available:
            logger.debug("Ollama недоступен")
            return None
        
        result = await self.ollama.analyze_pair(pair, timeframe, market_data)
        if result:
            result['provider'] = 'ollama'
        return result
    
    def get_status(self) -> Dict:
        """Получить статус провайдеров"""
        return {
            'mode': self.mode,
            'perplexity': self.perplexity.is_available,
            'ollama': self.ollama.is_available
        }
    
    def set_mode(self, mode: str) -> bool:
        """Изменить режим работы"""
        valid_modes = [self.PERPLEXITY_ONLY, self.LOCAL_ONLY, 
                       self.HYBRID, self.PREFER_LOCAL]
        if mode in valid_modes:
            self.mode = mode
            logger.info(f"🤖 LLM режим изменён на: {mode}")
            return True
        return False


# Глобальный экземпляр
llm_router = LLMRouter()
