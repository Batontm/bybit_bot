"""
Сервис анализа рынка и работы с LLM (Perplexity / Ollama)
"""
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from pybit.unified_trading import HTTP
from config.settings import logger, TIMEZONE
from config.api_config import BYBIT_TESTNET, get_pybit_kwargs
from config.trading_config import (
    MIN_ANALYSIS_INTERVAL_PER_PAIR,
    THRESHOLD_AUTO_TRADE,
    THRESHOLD_BUY,
    THRESHOLD_WAIT
)
from ..llm_router import llm_router
from ..db.llm_requests_repo import LLMRequestsRepository
from ..error_tracker import error_tracker
from .chart_service import chart_service
from .indicators_service import technical_indicators


class AnalysisService:
    """Сервис анализа монет и рыночных данных"""
    
    def __init__(self):
        self.llm_repo = LLMRequestsRepository()
        self.bybit_client = HTTP(**get_pybit_kwargs())
    
    async def analyze_pair(self, pair: str, timeframe: str = "1h", 
                          use_cache: bool = True) -> Optional[Dict]:
        """
        Провести анализ торговой пары
        
        Args:
            pair: Торговая пара
            timeframe: Таймфрейм для анализа
            use_cache: Использовать кэш если доступен
        
        Returns:
            Dict с анализом или None
        """
        # Проверяем кэш
        if use_cache:
            cached = self.llm_repo.get_latest_analysis(
                pair, 
                max_age_seconds=MIN_ANALYSIS_INTERVAL_PER_PAIR
            )
            
            if cached:
                logger.info(f"📦 Используем кэшированный анализ: {pair}")
                return {
                    'pair': cached['pair'],
                    'timeframe': cached['timeframe'],
                    'score': cached['score'],
                    'signal': cached['signal'],
                    'summary': cached['summary'],
                    'cached': True,
                    'timestamp': cached['created_at']
                }
        
        # Получаем рыночные данные
        market_data = self.get_market_data(pair)
        
        if not market_data:
            logger.error(f"❌ Не удалось получить данные для {pair}")
            return None
        
        # Запрашиваем анализ у LLM Router (Perplexity / Ollama)
        analysis = await llm_router.analyze_pair(pair, timeframe, market_data)
        
        if analysis:
            for k, v in market_data.items():
                analysis.setdefault(k, v)
            analysis['pair'] = pair
            analysis['timeframe'] = timeframe
            analysis['cached'] = False
            analysis['timestamp'] = datetime.now(TIMEZONE).isoformat()
        
        return analysis
    
    def get_market_data(self, pair: str) -> Optional[Dict]:
        """Получить текущие рыночные данные для пары"""
        try:
            # Получаем тикер
            ticker = self.bybit_client.get_tickers(
                category="spot",
                symbol=pair
            )
            
            if ticker['retCode'] != 0:
                return None
            
            data = ticker['result']['list'][0]
            
            price = float(data['lastPrice'])
            change_24h = float(data['price24hPcnt']) * 100
            volume_24h = float(data['volume24h'])
            
            # Определяем тренд по изменению цены
            if change_24h > 8:
                trend = "восходящий"
            elif change_24h < -2:
                trend = "нисходящий"
            else:
                trend = "боковой"
            
            market_data = {
                'price': price,
                'change_24h': round(change_24h, 2),
                'volume_24h': volume_24h,
                'trend': trend,
                'high_24h': float(data['highPrice24h']),
                'low_24h': float(data['lowPrice24h'])
            }
            
            # Добавляем технические индикаторы
            klines = self.get_klines(pair, interval="60", limit=60)
            if klines and len(klines) >= 50:
                closes = [float(k['close']) for k in klines]
                tech_analysis = technical_indicators.analyze(closes)
                
                market_data['rsi'] = tech_analysis.get('rsi')
                market_data['ema_trend'] = tech_analysis.get('ema_trend')
                market_data['macd_signal'] = tech_analysis.get('macd_signal')
                market_data['tech_score'] = tech_analysis.get('overall_score')
                market_data['tech_signal'] = tech_analysis.get('overall_signal')
                market_data['bb_upper'] = tech_analysis.get('bb_upper')
                market_data['bb_middle'] = tech_analysis.get('bb_middle')
                market_data['bb_lower'] = tech_analysis.get('bb_lower')
                
                logger.debug(f"📐 Индикаторы {pair}: RSI={tech_analysis.get('rsi')}, EMA={tech_analysis.get('ema_trend')}, MACD={tech_analysis.get('macd_signal')}")
            
            logger.debug(f"📊 Рыночные данные {pair}: {price} ({change_24h:+.2f}%)")
            
            return market_data
            
        except Exception as e:
            error_tracker.add_error("Bybit", "MarketDataError", str(e))
            logger.error(f"❌ Ошибка получения данных {pair}: {e}")
            return None
    
    
    def get_klines(self, pair: str, interval: str = "60", limit: int = 50) -> List[Dict]:
        """Получить исторические свечи"""
        try:
            # interval: 1, 3, 5, 15, 30, 60, 120, 240, 360, 720, D, M, W
            response = self.bybit_client.get_kline(
                category="spot",
                symbol=pair,
                interval=interval,
                limit=limit
            )
            
            if response['retCode'] != 0:
                return []
                
            # Bybit возвращает: [startTime, open, high, low, close, volume, turnover]
            # Нужно преобразовать в dict
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
            
            # Bybit возвращает от новых к старым, нам часто нужно наоборот для графика
            return sorted(klines, key=lambda x: int(x['startTime']))
            
        except Exception as e:
            logger.error(f"❌ Ошибка получения свечей {pair}: {e}")
            return []

    def check_order_book_imbalance(self, pair: str) -> bool:
        """
        Проверить дисбаланс в стакане.
        Возвращает False, если есть сильная "стена" продавцов.
        """
        try:
            # Получаем стакан глубиной 50
            depth = self.bybit_client.get_orderbook(
                category="spot",
                symbol=pair,
                limit=50
            )
            
            if depth['retCode'] != 0:
                logger.warning(f"⚠️ Не удалось получить стакан {pair}")
                return True # Пропускаем, если ошибка
            
            bids = depth['result']['b']
            asks = depth['result']['a']
            
            if not bids or not asks:
                return True
            
            # Считаем объемы (цена * кол-во) или просто кол-во?
            # Лучше просто кол-во базового актива для оценки давления
            bid_vol = sum(float(b[1]) for b in bids)
            ask_vol = sum(float(a[1]) for a in asks)
            
            if bid_vol == 0:
                return False
                
            ratio = ask_vol / bid_vol
            
            if ratio > 5.0:
                logger.warning(f"🧱 Стена продавцов {pair}: Ask/Bid = {ratio:.2f} (Ask: {ask_vol:.1f}, Bid: {bid_vol:.1f})")
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка анализа стакана {pair}: {e}")
            return True

    async def get_analysis_chart(self, pair: str, analysis: Dict) -> Optional[bytes]:
        """Получить изображение графика для анализа"""
        klines = self.get_klines(pair, interval="60", limit=50) # 1h candles
        if not klines:
            return None
            
        return chart_service.generate_chart(pair, klines, analysis)

    def should_enter_trade(self, analysis: Dict) -> tuple[bool, str]:
        """
        Определить, стоит ли входить в сделку
        
        Returns:
            (можно_входить, причина)
        """
        if not analysis:
            return False, "Анализ недоступен"
        
        score = analysis.get('score', 0)
        signal = analysis.get('signal', 'WAIT')

        # Жёсткие veto (AI не может обойти): защита от покупок на хаях / FOMO
        rsi = analysis.get('rsi')
        if rsi is not None:
            try:
                rsi_val = float(rsi)
                if rsi_val > 70:
                    return False, f"VETO: RSI слишком высокий ({rsi_val:.2f} > 70)"
            except Exception:
                pass

        change_24h = analysis.get('change_24h')
        if change_24h is not None:
            try:
                chg_val = float(change_24h)
                if chg_val > 5:
                    return False, f"VETO: Рост за 24h слишком высокий (+{chg_val:.2f}%) — риск FOMO"
            except Exception:
                pass

        price = analysis.get('price')
        bb_upper = analysis.get('bb_upper')
        if price is not None and bb_upper is not None:
            try:
                price_val = float(price)
                upper_val = float(bb_upper)
                if price_val >= upper_val:
                    return False, f"VETO: Цена у/выше верхней BB ({price_val:.6f} >= {upper_val:.6f}) — вход запрещён"
            except Exception:
                pass
        
        if signal == 'AVOID':
            return False, f"Сигнал AVOID (score={score})"
        
        if score < THRESHOLD_BUY:
            return False, f"Score слишком низкий ({score} < {THRESHOLD_BUY})"
            
        # Проверка стакана перед одобрением
        if not self.check_order_book_imbalance(analysis['pair']):
            return False, "Обнаружена стена продавцов (Imbalance > 5)"
        
        if score >= THRESHOLD_AUTO_TRADE:
            return True, f"Сильный сигнал (score={score})"
        
        return False, f"Score недостаточен для авто-входа ({score} < {THRESHOLD_AUTO_TRADE})"
    
    def format_analysis_message(self, analysis: Dict) -> str:
        """Форматировать анализ для отправки в Telegram"""
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
        
        message = f"""📊 Анализ {pair}

{signal_emoji.get(signal, '⚪')} Сигнал: {signal}
⭐ Score: {score}/100

📝 Резюме:
{summary}

{recommendation}

{cache_status}
"""
        
        return message


# Глобальный экземпляр
analysis_service = AnalysisService()
