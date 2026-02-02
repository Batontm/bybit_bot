"""
Сервис технических индикаторов (RSI, MACD, EMA)
"""
import numpy as np
from typing import Dict, List, Optional, Tuple
from config.settings import logger


class TechnicalIndicators:
    """Расчёт технических индикаторов"""
    
    @staticmethod
    def calculate_rsi(closes: List[float], period: int = 14) -> Optional[float]:
        """
        Расчёт RSI (Relative Strength Index)
        
        Args:
            closes: Список цен закрытия (от старых к новым)
            period: Период RSI (по умолчанию 14)
            
        Returns:
            RSI значение (0-100) или None
        """
        if len(closes) < period + 1:
            return None
            
        try:
            closes_arr = np.array(closes, dtype=float)
            deltas = np.diff(closes_arr)
            
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            
            avg_gain = np.mean(gains[-period:])
            avg_loss = np.mean(losses[-period:])
            
            if avg_loss == 0:
                return 100.0
            
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            
            return round(rsi, 2)
            
        except Exception as e:
            logger.error(f"❌ Ошибка расчёта RSI: {e}")
            return None
    
    @staticmethod
    def calculate_atr(highs: List[float], lows: List[float], closes: List[float], 
                      period: int = 14) -> Optional[float]:
        """
        Расчёт ATR (Average True Range) - индикатор волатильности
        
        Args:
            highs: Список максимальных цен
            lows: Список минимальных цен
            closes: Список цен закрытия
            period: Период ATR (по умолчанию 14)
            
        Returns:
            ATR значение в USDT или None
        """
        if len(closes) < period + 1:
            return None
            
        try:
            tr_values = []
            for i in range(1, len(closes)):
                high = highs[i]
                low = lows[i]
                prev_close = closes[i - 1]
                
                # True Range = max из трёх значений
                tr = max(
                    high - low,                    # Диапазон свечи
                    abs(high - prev_close),        # Гэп вверх
                    abs(low - prev_close)          # Гэп вниз
                )
                tr_values.append(tr)
            
            # ATR = SMA от True Range за период
            if len(tr_values) >= period:
                atr = sum(tr_values[-period:]) / period
                return round(atr, 4)
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка расчёта ATR: {e}")
            return None
    
    @staticmethod
    def calculate_ema(closes: List[float], period: int) -> Optional[float]:
        """
        Расчёт EMA (Exponential Moving Average)
        
        Args:
            closes: Список цен закрытия
            period: Период EMA
            
        Returns:
            Значение EMA или None
        """
        if len(closes) < period:
            return None
            
        try:
            closes_arr = np.array(closes, dtype=float)
            multiplier = 2 / (period + 1)
            
            # Начальное значение - SMA
            ema = np.mean(closes_arr[:period])
            
            # Расчёт EMA
            for price in closes_arr[period:]:
                ema = (price - ema) * multiplier + ema
                
            return round(ema, 4)
            
        except Exception as e:
            logger.error(f"❌ Ошибка расчёта EMA: {e}")
            return None
    
    @staticmethod
    def calculate_macd(closes: List[float], 
                       fast_period: int = 12, 
                       slow_period: int = 26, 
                       signal_period: int = 9) -> Optional[Dict]:
        """
        Расчёт MACD (Moving Average Convergence Divergence)
        
        Returns:
            Dict с macd, signal, histogram или None
        """
        if len(closes) < slow_period + signal_period:
            return None
            
        try:
            closes_arr = np.array(closes, dtype=float)
            
            # EMA для MACD
            def ema_arr(data, period):
                multiplier = 2 / (period + 1)
                ema = [np.mean(data[:period])]
                for price in data[period:]:
                    ema.append((price - ema[-1]) * multiplier + ema[-1])
                return ema
            
            # Fast и Slow EMA
            fast_ema = ema_arr(closes_arr, fast_period)
            slow_ema = ema_arr(closes_arr, slow_period)
            
            # Выравниваем длину
            min_len = min(len(fast_ema), len(slow_ema))
            fast_ema = fast_ema[-min_len:]
            slow_ema = slow_ema[-min_len:]
            
            # MACD Line
            macd_line = np.array(fast_ema) - np.array(slow_ema)
            
            # Signal Line (EMA от MACD)
            if len(macd_line) >= signal_period:
                signal_ema = ema_arr(macd_line.tolist(), signal_period)
                signal = signal_ema[-1]
                macd = macd_line[-1]
                histogram = macd - signal
                
                return {
                    'macd': round(macd, 4),
                    'signal': round(signal, 4),
                    'histogram': round(histogram, 4)
                }
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка расчёта MACD: {e}")
            return None

    @staticmethod
    def calculate_bollinger_bands(
        closes: List[float],
        period: int = 20,
        stddev: float = 2.0
    ) -> Optional[Dict]:
        if len(closes) < period:
            return None

        try:
            window = np.array(closes[-period:], dtype=float)
            sma = float(np.mean(window))
            sd = float(np.std(window))
            upper = sma + (stddev * sd)
            lower = sma - (stddev * sd)
            return {
                'middle': round(sma, 6),
                'upper': round(upper, 6),
                'lower': round(lower, 6),
            }
        except Exception as e:
            logger.error(f"❌ Ошибка расчёта Bollinger Bands: {e}")
            return None
    
    @staticmethod
    def get_ema_trend(closes: List[float]) -> str:
        """
        Определить тренд по EMA20/EMA50
        
        Returns:
            'BULLISH', 'BEARISH' или 'NEUTRAL'
        """
        ema20 = TechnicalIndicators.calculate_ema(closes, 20)
        ema50 = TechnicalIndicators.calculate_ema(closes, 50)
        
        if ema20 is None or ema50 is None:
            return "NEUTRAL"
        
        current_price = closes[-1] if closes else 0
        
        if ema20 > ema50 and current_price > ema20:
            return "BULLISH"
        elif ema20 < ema50 and current_price < ema20:
            return "BEARISH"
        else:
            return "NEUTRAL"
    
    @staticmethod
    def get_rsi_signal(rsi: float) -> str:
        """Интерпретация RSI"""
        if rsi is None:
            return "N/A"
        if rsi >= 70:
            return "OVERBOUGHT"
        elif rsi <= 30:
            return "OVERSOLD"
        elif rsi >= 50:
            return "BULLISH"
        else:
            return "BEARISH"
    
    @staticmethod
    def get_macd_signal(macd_data: Dict) -> str:
        """Интерпретация MACD"""
        if macd_data is None:
            return "N/A"
        
        histogram = macd_data.get('histogram', 0)
        
        if histogram > 0:
            return "BULLISH"
        elif histogram < 0:
            return "BEARISH"
        else:
            return "NEUTRAL"
    
    @staticmethod
    def analyze(closes: List[float]) -> Dict:
        """
        Полный технический анализ
        
        Returns:
            Dict со всеми индикаторами и общим сигналом
        """
        result = {
            'rsi': None,
            'rsi_signal': 'N/A',
            'ema20': None,
            'ema50': None,
            'ema_trend': 'NEUTRAL',
            'macd': None,
            'macd_signal': 'N/A',
            'bb_upper': None,
            'bb_middle': None,
            'bb_lower': None,
            'overall_score': 0,
            'overall_signal': 'NEUTRAL'
        }
        
        if not closes or len(closes) < 50:
            return result
        
        # RSI
        rsi = TechnicalIndicators.calculate_rsi(closes)
        result['rsi'] = rsi
        result['rsi_signal'] = TechnicalIndicators.get_rsi_signal(rsi)
        
        # EMA
        result['ema20'] = TechnicalIndicators.calculate_ema(closes, 20)
        result['ema50'] = TechnicalIndicators.calculate_ema(closes, 50)
        result['ema_trend'] = TechnicalIndicators.get_ema_trend(closes)
        
        # MACD
        macd_data = TechnicalIndicators.calculate_macd(closes)
        result['macd'] = macd_data
        result['macd_signal'] = TechnicalIndicators.get_macd_signal(macd_data)

        # Bollinger Bands
        bb = TechnicalIndicators.calculate_bollinger_bands(closes)
        if bb:
            result['bb_upper'] = bb.get('upper')
            result['bb_middle'] = bb.get('middle')
            result['bb_lower'] = bb.get('lower')
        
        # Общий скор на основе индикаторов
        score = 50  # Базовый
        
        # RSI влияние
        if rsi:
            if 40 <= rsi <= 60:
                score += 10  # Нейтральная зона - хорошо
            elif 30 <= rsi < 40:
                score += 15  # Возможен отскок
            elif rsi < 30:
                score += 5   # Перепроданность - риск
            elif 60 < rsi <= 70:
                score += 5   # Сила, но осторожно
            elif rsi > 70:
                score -= 10  # Перекупленность
        
        # EMA влияние
        if result['ema_trend'] == 'BULLISH':
            score += 15
        elif result['ema_trend'] == 'BEARISH':
            score -= 15
        
        # MACD влияние
        if result['macd_signal'] == 'BULLISH':
            score += 10
        elif result['macd_signal'] == 'BEARISH':
            score -= 10
        
        result['overall_score'] = max(0, min(100, score))
        
        if result['overall_score'] >= 65:
            result['overall_signal'] = 'BULLISH'
        elif result['overall_score'] <= 35:
            result['overall_signal'] = 'BEARISH'
        else:
            result['overall_signal'] = 'NEUTRAL'
        
        return result


# Глобальный экземпляр
technical_indicators = TechnicalIndicators()
