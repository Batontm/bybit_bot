"""Тесты `_calculate_score` пре-фильтра — pure-функция ранжирования."""
import pytest

from bot.services.prefilter_service import PreFilterService


@pytest.fixture
def svc():
    return PreFilterService()


def test_score_in_valid_range(svc):
    """Score всегда в [0, 100] независимо от входов."""
    s = svc._calculate_score({'rsi': 99, 'ema_trend': 'BEARISH', 'macd_signal': 'BEARISH'}, 0.1)
    assert 0 <= s <= 100

    s = svc._calculate_score({'rsi': 35, 'ema_trend': 'BULLISH', 'macd_signal': 'BULLISH'}, 5.0)
    assert 0 <= s <= 100


def test_ideal_setup_high_score(svc):
    """RSI 35 + BULLISH EMA + BULLISH MACD + объём 1.5x → score высокий."""
    s = svc._calculate_score(
        {'rsi': 35, 'ema_trend': 'BULLISH', 'macd_signal': 'BULLISH'},
        volume_ratio=1.5,
    )
    # 50 (база) + 20 (RSI зона) + 15 (EMA) + 10 (MACD) + 15 (volume) = 110 → clamp 100
    assert s == 100


def test_bearish_setup_low_score(svc):
    """BEARISH EMA + BEARISH MACD → score падает."""
    s = svc._calculate_score(
        {'rsi': 50, 'ema_trend': 'BEARISH', 'macd_signal': 'BEARISH'},
        volume_ratio=0.8,
    )
    # 50 + 10 (RSI 50) - 10 (EMA) - 5 (MACD) = 45 (объём не штрафует, только бонусы)
    assert s == 45


def test_rsi_buckets(svc):
    """Бонус за RSI зависит от диапазона."""
    base = {'ema_trend': 'NEUTRAL', 'macd_signal': 'NEUTRAL'}
    # 30-45: +20
    s_ideal = svc._calculate_score({**base, 'rsi': 35}, volume_ratio=1.0)
    # 45-60: +10
    s_mid = svc._calculate_score({**base, 'rsi': 55}, volume_ratio=1.0)
    # < 30: +5
    s_oversold = svc._calculate_score({**base, 'rsi': 25}, volume_ratio=1.0)
    # > 60: 0
    s_high = svc._calculate_score({**base, 'rsi': 70}, volume_ratio=1.0)

    assert s_ideal > s_mid > s_oversold > s_high


def test_volume_boost(svc):
    """Высокий volume_ratio даёт бонус."""
    base = {'rsi': 50, 'ema_trend': 'NEUTRAL', 'macd_signal': 'NEUTRAL'}
    s_low = svc._calculate_score(base, volume_ratio=0.5)
    s_normal = svc._calculate_score(base, volume_ratio=1.0)
    s_high = svc._calculate_score(base, volume_ratio=1.3)
    s_very_high = svc._calculate_score(base, volume_ratio=2.0)

    assert s_low == s_normal  # ниже 1.2x — без бонуса
    assert s_high > s_normal
    assert s_very_high > s_high


def test_missing_rsi_uses_default(svc):
    """Отсутствие RSI не должно валить расчёт."""
    s = svc._calculate_score(
        {'ema_trend': 'NEUTRAL', 'macd_signal': 'NEUTRAL'},
        volume_ratio=1.0,
    )
    # default rsi=50 → бонус 0 (выше зоны 30-45 и 45-60? 50 попадает в 45-60 → +10)
    # 50 + 10 = 60
    assert s == 60
