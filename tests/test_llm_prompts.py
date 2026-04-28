"""Тесты парсера и билдера промптов LLM."""
import pytest

from bot.llm.prompts import build_analysis_prompt, parse_analysis


# ============= parse_analysis =============

def test_parse_clean_response():
    """Идеальный ответ от модели парсится корректно."""
    content = """SCORE: 72
SIGNAL: BUY
TARGET: 105500.5
STOP_LOSS: 102100
LOGIC: RSI 58 в норме, объём растёт. Тренд EMA восходящий."""
    r = parse_analysis(content)
    assert r['score'] == 72
    assert r['signal'] == 'BUY'
    assert r['target'] == 105500.5
    assert r['stop_loss'] == 102100
    assert 'RSI' in r['summary']


def test_parse_with_markdown_and_currency():
    """Markdown-обёртки и валютные символы не ломают парсер."""
    content = """Some preamble
**SCORE:** 45
SIGNAL: WAIT
TARGET: $104,200
SL: 102000
LOGIC: Перекуп."""
    r = parse_analysis(content)
    assert r['score'] == 45
    assert r['signal'] == 'WAIT'
    assert r['target'] == 104200.0
    assert r['stop_loss'] == 102000


def test_parse_signal_aliases():
    """Алиасы LONG/SHORT/SELL приводятся к BUY/AVOID."""
    long_resp = "SCORE: 70\nSIGNAL: LONG\nLOGIC: ok"
    assert parse_analysis(long_resp)['signal'] == 'BUY'

    short_resp = "SCORE: 60\nSIGNAL: SHORT\nLOGIC: ok"
    assert parse_analysis(short_resp)['signal'] == 'AVOID'

    sell_resp = "SCORE: 55\nSIGNAL: SELL\nLOGIC: ok"
    assert parse_analysis(sell_resp)['signal'] == 'AVOID'


def test_low_score_wait_promoted_to_avoid():
    """SCORE < 40 + SIGNAL=WAIT → AVOID (защита от слабых сигналов)."""
    content = "SCORE: 30\nSIGNAL: WAIT\nLOGIC: bad"
    assert parse_analysis(content)['signal'] == 'AVOID'


def test_parse_score_with_slash():
    """SCORE: 72/100 — берём числитель."""
    content = "SCORE: 72/100\nSIGNAL: BUY\nLOGIC: ok"
    assert parse_analysis(content)['score'] == 72


def test_parse_multiline_logic():
    """LOGIC может занимать несколько строк."""
    content = """SCORE: 65
SIGNAL: BUY
TARGET: 100
STOP_LOSS: 95
LOGIC: Первое предложение.
Второе предложение продолжается."""
    r = parse_analysis(content)
    assert 'Первое' in r['summary']
    assert 'Второе' in r['summary']


def test_parse_empty_input_safe_defaults():
    """Пустой/невалидный вход не падает, возвращает безопасные дефолты."""
    r = parse_analysis("")
    assert r['score'] == 50
    # 50 >= 40, поэтому остаётся WAIT (не повышается до AVOID)
    assert r['signal'] == 'WAIT'
    assert r['target'] == 0.0
    assert r['summary']  # непустой fallback-текст


def test_parse_garbage_score_fallback():
    """Невалидный SCORE не должен валить парсер."""
    content = "SCORE: abc\nSIGNAL: BUY\nLOGIC: ok"
    r = parse_analysis(content)
    assert r['score'] == 50  # дефолт


# ============= build_analysis_prompt =============

def test_prompt_contains_pair_and_timeframe():
    market = {'price': 100, 'change_24h': 1.2, 'volume_24h': 1e9}
    p = build_analysis_prompt('BTCUSDT', '1h', market)
    assert 'BTCUSDT' in p
    assert '1h' in p


def test_prompt_includes_indicators_when_present():
    market = {
        'price': 100, 'change_24h': 1.2, 'volume_24h': 1e9,
        'rsi': 55, 'macd_signal': 'BULLISH', 'ema_trend': 'BULLISH',
    }
    p = build_analysis_prompt('BTCUSDT', '1h', market)
    assert 'RSI' in p
    assert '55' in p
    assert 'BULLISH' in p


def test_prompt_omits_indicators_when_missing():
    market = {'price': 100, 'change_24h': 1.2, 'volume_24h': 1e9}
    p = build_analysis_prompt('BTCUSDT', '1h', market)
    # Когда RSI не передан — блок индикаторов не должен появляться
    assert 'RSI(14):' not in p


def test_web_search_block_added_only_when_requested():
    market = {'price': 100, 'change_24h': 1.2, 'volume_24h': 1e9}
    no_ws = build_analysis_prompt('BTCUSDT', '1h', market, with_web_search=False)
    with_ws = build_analysis_prompt('BTCUSDT', '1h', market, with_web_search=True)
    assert 'web-search' not in no_ws.lower()
    assert 'web-search' in with_ws.lower()
