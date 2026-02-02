"""
Pre-filter сервис для технического анализа перед отправкой в AI
Стратегия "Воронка": сначала Python фильтр, потом Perplexity
"""
from typing import List, Dict, Optional, Tuple
from pybit.unified_trading import HTTP
from config.settings import logger
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET
from .indicators_service import technical_indicators


class PreFilterService:
    """
    Двухэтапный фильтр для экономии лимитов Perplexity:
    
    Этап 1 (Python): Быстрый технический анализ
    Этап 2 (AI): Только лучшие кандидаты отправляются в Perplexity
    """
    
    # Критерии прохождения фильтра
    MIN_RSI = 25           # RSI не ниже (не слишком перепродано, может падать дальше)
    MAX_RSI = 70           # RSI не выше (не перекуплено)
    MIN_VOLUME_CHANGE = 1.2  # Объём должен быть выше среднего на 20%
    
    def __init__(self):
        # Используем mainnet для получения реальных рыночных данных
        # (только чтение klines, без торговых операций)
        self.client = HTTP(
            testnet=False  # Mainnet для реальных данных RSI/MACD
        )
    
    def scan_and_filter(self, pairs: List[str], top_n: int = 2) -> List[Dict]:
        """
        Сканировать пары и вернуть топ N кандидатов для AI анализа
        
        Args:
            pairs: Список пар для проверки
            top_n: Сколько лучших вернуть
            
        Returns:
            List[Dict] с парами и их техническими данными
        """
        candidates = []
        
        for pair in pairs:
            try:
                score, analysis = self._analyze_pair(pair)
                
                if score > 0:
                    candidates.append({
                        'pair': pair,
                        'score': score,
                        'analysis': analysis
                    })
                    logger.debug(f"✅ Pre-filter {pair}: score={score:.0f}")
                else:
                    logger.debug(f"❌ Pre-filter {pair}: отфильтрован ({analysis.get('reason', 'unknown')})")
                    
            except Exception as e:
                logger.debug(f"⚠️ Pre-filter {pair}: ошибка - {e}")
        
        # Сортируем по score и берём топ N
        candidates.sort(key=lambda x: x['score'], reverse=True)
        top_candidates = candidates[:top_n]
        
        if top_candidates:
            logger.info(f"🎯 Pre-filter: топ-{len(top_candidates)} кандидатов для AI:")
            for c in top_candidates:
                logger.info(f"   📊 {c['pair']}: score={c['score']:.0f} | RSI={c['analysis'].get('rsi', 'N/A')}")
        else:
            logger.info("ℹ️ Pre-filter: нет подходящих кандидатов")
        
        return top_candidates
    
    def _analyze_pair(self, pair: str) -> Tuple[float, Dict]:
        """
        Технический анализ пары
        
        Returns:
            (score, analysis_dict)
            score = 0 означает отфильтровано
        """
        analysis = {'pair': pair}
        
        try:
            # 1. Получаем klines для технических индикаторов
            klines = self._get_klines(pair, interval="60", limit=60)
            
            if not klines or len(klines) < 50:
                analysis['reason'] = 'недостаточно данных'
                return 0, analysis
            
            closes = [float(k['close']) for k in klines]
            volumes = [float(k['volume']) for k in klines]
            
            # 2. Расчёт индикаторов
            tech = technical_indicators.analyze(closes)
            
            analysis.update({
                'rsi': tech.get('rsi'),
                'ema_trend': tech.get('ema_trend'),
                'macd_signal': tech.get('macd_signal'),
                'tech_score': tech.get('overall_score', 50)
            })
            
            # 3. Проверка RSI
            rsi = tech.get('rsi')
            if rsi is None:
                analysis['reason'] = 'RSI не рассчитан'
                return 0, analysis
            
            if rsi < self.MIN_RSI:
                analysis['reason'] = f'RSI слишком низкий ({rsi:.0f} < {self.MIN_RSI})'
                return 0, analysis
            
            if rsi > self.MAX_RSI:
                analysis['reason'] = f'RSI слишком высокий ({rsi:.0f} > {self.MAX_RSI})'
                return 0, analysis
            
            # 4. Проверка объёма (последние 5 свечей vs средний)
            recent_volume = sum(volumes[-5:]) / 5
            avg_volume = sum(volumes) / len(volumes)
            volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 0
            
            analysis['volume_ratio'] = round(volume_ratio, 2)
            
            if volume_ratio < self.MIN_VOLUME_CHANGE:
                analysis['reason'] = f'Объём ниже среднего ({volume_ratio:.2f}x)'
                # Не отфильтровываем, но снижаем score
            
            # 5. Расчёт итогового score
            score = self._calculate_score(tech, volume_ratio)
            analysis['pre_filter_score'] = score
            
            return score, analysis
            
        except Exception as e:
            analysis['reason'] = str(e)
            return 0, analysis
    
    def _calculate_score(self, tech: Dict, volume_ratio: float) -> float:
        """Расчёт score для ранжирования кандидатов"""
        score = 50  # Базовый
        
        # RSI бонусы (идеальная зона 30-50)
        rsi = tech.get('rsi', 50)
        if 30 <= rsi <= 45:
            score += 20  # Идеальная зона для входа
        elif 45 < rsi <= 60:
            score += 10
        elif rsi < 30:
            score += 5  # Перепроданность - может отскочить
        
        # EMA тренд
        if tech.get('ema_trend') == 'BULLISH':
            score += 15
        elif tech.get('ema_trend') == 'BEARISH':
            score -= 10
        
        # MACD
        if tech.get('macd_signal') == 'BULLISH':
            score += 10
        elif tech.get('macd_signal') == 'BEARISH':
            score -= 5
        
        # Объём
        if volume_ratio >= 1.5:
            score += 15  # Сильно повышенный объём
        elif volume_ratio >= 1.2:
            score += 5
        
        return max(0, min(100, score))
    
    def _get_klines(self, pair: str, interval: str = "60", limit: int = 60) -> List[Dict]:
        """Получить свечи"""
        try:
            response = self.client.get_kline(
                category="spot",
                symbol=pair,
                interval=interval,
                limit=limit
            )
            
            if response['retCode'] != 0:
                return []
            
            klines = []
            for k in response['result']['list']:
                klines.append({
                    'startTime': k[0],
                    'open': k[1],
                    'high': k[2],
                    'low': k[3],
                    'close': k[4],
                    'volume': k[5]
                })
            
            return sorted(klines, key=lambda x: int(x['startTime']))
            
        except Exception as e:
            logger.error(f"❌ Ошибка получения klines {pair}: {e}")
            return []


# Глобальный экземпляр
prefilter_service = PreFilterService()
