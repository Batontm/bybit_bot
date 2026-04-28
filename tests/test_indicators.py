"""Тесты технических индикаторов (RSI/EMA/MACD/ATR/BB)."""
import math

import pytest

from bot.services.indicators_service import TechnicalIndicators as TI


# ============= RSI =============

def test_rsi_returns_none_for_short_input():
    assert TI.calculate_rsi([1, 2, 3], period=14) is None


def test_rsi_all_gains_returns_100():
    """Стабильный рост → RSI = 100 (avg_loss=0)."""
    closes = list(range(1, 30))  # монотонно растёт
    rsi = TI.calculate_rsi(closes, period=14)
    assert rsi == 100.0


def test_rsi_in_valid_range():
    """Случайные близкие цены → RSI в [0, 100]."""
    closes = [100 + (i % 5) - 2 for i in range(30)]
    rsi = TI.calculate_rsi(closes, period=14)
    assert rsi is not None
    assert 0 <= rsi <= 100


def test_rsi_falling_market_below_50():
    """Падающий рынок → RSI < 50."""
    closes = [100 - i * 0.5 for i in range(30)]
    rsi = TI.calculate_rsi(closes, period=14)
    assert rsi is not None
    assert rsi < 50


# ============= EMA =============

def test_ema_returns_none_for_short_input():
    assert TI.calculate_ema([1, 2, 3], period=20) is None


def test_ema_constant_series_equals_value():
    """Если все цены равны 100 — EMA тоже 100."""
    closes = [100.0] * 50
    ema = TI.calculate_ema(closes, period=20)
    assert ema == 100.0


def test_ema_responds_to_recent_changes():
    """EMA20 ближе к недавним ценам, чем EMA50."""
    closes = [100.0] * 30 + [200.0] * 30  # резкий скачок
    ema20 = TI.calculate_ema(closes, period=20)
    ema50 = TI.calculate_ema(closes, period=50)
    assert ema20 > ema50  # EMA20 ближе к новым (высоким) ценам


def test_ema_trend_bullish():
    """Восходящий ряд + цена выше EMA20 > EMA50 → BULLISH."""
    closes = [100 + i * 0.5 for i in range(60)]
    assert TI.get_ema_trend(closes) == 'BULLISH'


def test_ema_trend_bearish():
    closes = [100 - i * 0.5 for i in range(60)]
    assert TI.get_ema_trend(closes) == 'BEARISH'


def test_ema_trend_neutral_for_short():
    assert TI.get_ema_trend([100, 101]) == 'NEUTRAL'


# ============= MACD =============

def test_macd_returns_none_for_short_input():
    assert TI.calculate_macd([1] * 20) is None


def test_macd_constant_series_yields_zero():
    """Константа → fast_ema == slow_ema → MACD ≈ 0."""
    closes = [100.0] * 50
    macd = TI.calculate_macd(closes)
    assert macd is not None
    assert abs(macd['macd']) < 0.01
    assert abs(macd['histogram']) < 0.01


def test_macd_signal_bullish_on_breakout():
    """Плоский рынок → резкий рост → гистограмма положительная."""
    closes = [100.0] * 30 + [100 + i * 1.5 for i in range(1, 31)]
    macd = TI.calculate_macd(closes)
    assert macd is not None
    assert TI.get_macd_signal(macd) == 'BULLISH'


def test_macd_signal_bearish_on_breakdown():
    """Плоский рынок → резкое падение → гистограмма отрицательная."""
    closes = [100.0] * 30 + [100 - i * 1.5 for i in range(1, 31)]
    macd = TI.calculate_macd(closes)
    assert macd is not None
    assert TI.get_macd_signal(macd) == 'BEARISH'


# ============= ATR =============

def test_atr_returns_none_for_short_input():
    assert TI.calculate_atr([1] * 5, [1] * 5, [1] * 5, period=14) is None


def test_atr_constant_zero_range():
    """Если high=low=close — ATR = 0."""
    n = 20
    closes = [100.0] * n
    atr = TI.calculate_atr(closes, closes, closes, period=14)
    assert atr == 0.0


def test_atr_positive_for_volatile():
    n = 30
    highs = [102 + (i % 3) for i in range(n)]
    lows = [98 - (i % 3) for i in range(n)]
    closes = [100 + (i % 5) - 2 for i in range(n)]
    atr = TI.calculate_atr(highs, lows, closes, period=14)
    assert atr is not None and atr > 0


# ============= Bollinger Bands =============

def test_bb_returns_none_for_short_input():
    assert TI.calculate_bollinger_bands([1, 2, 3], period=20) is None


def test_bb_middle_is_sma():
    """Средняя линия = SMA окна."""
    closes = list(range(1, 21))
    bb = TI.calculate_bollinger_bands(closes, period=20, stddev=2.0)
    assert bb is not None
    expected_sma = sum(closes) / 20
    assert abs(bb['middle'] - expected_sma) < 1e-6


def test_bb_upper_above_middle_above_lower():
    closes = [100 + (i % 5) for i in range(25)]
    bb = TI.calculate_bollinger_bands(closes, period=20, stddev=2.0)
    assert bb['upper'] > bb['middle'] > bb['lower']


# ============= analyze() (integration) =============

def test_analyze_short_input_returns_defaults():
    result = TI.analyze([1, 2, 3])
    assert result['rsi'] is None
    assert result['overall_score'] == 0
    assert result['overall_signal'] == 'NEUTRAL'


def test_analyze_full_pipeline_uptrend():
    closes = [100 + i * 0.3 for i in range(60)]
    result = TI.analyze(closes)
    assert result['rsi'] is not None
    assert result['ema_trend'] == 'BULLISH'
    # Восходящий тренд → score должен быть выше базовых 50
    assert result['overall_score'] > 50


def test_analyze_score_clamped_to_0_100():
    closes = [100 + i * 0.3 for i in range(60)]
    result = TI.analyze(closes)
    assert 0 <= result['overall_score'] <= 100


# ============= rsi_signal interpretations =============

@pytest.mark.parametrize("rsi,expected", [
    (None, 'N/A'),
    (75, 'OVERBOUGHT'),
    (70, 'OVERBOUGHT'),
    (55, 'BULLISH'),
    (50, 'BULLISH'),
    (40, 'BEARISH'),
    (25, 'OVERSOLD'),
])
def test_rsi_signal_buckets(rsi, expected):
    assert TI.get_rsi_signal(rsi) == expected
