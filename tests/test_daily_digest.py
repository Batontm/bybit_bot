"""Ежедневная сводка: текст, расстояние до сигнала тренда, устойчивость к сбоям источников."""
from datetime import datetime, timedelta

from config.settings import TIMEZONE
from bot.services import daily_digest as DD


def _base():
    return {
        'now': TIMEZONE.localize(datetime(2026, 11, 13, 9, 0)),
        'version': 'v9.9', 'mode': 'ACTIVE', 'auto_trading': True,
        'uptime': timedelta(days=2, hours=3),
        'regime': {'allowed': False, 'label': 'КРАСНЫЙ', 'price': 71721.8, 'ema200': 78497.3, 'rsi': 27.35},
        'equity': 155340.8, 'equity_peak': 155422.5,
        'positions': [], 'closed_24h': {'count': 0, 'pnl': 0.0},
        'trend': [{'pair': 'SOLUSDT', 'close': 83.27, 'need': 107.49, 'pct': 29.1},
                  {'pair': 'BTCUSDT', 'close': 74013.6, 'need': 84374.1, 'pct': 14.0}],
        'problems': [],
    }


def test_trend_distance_uses_higher_of_breakout_and_ema():
    d = DD.trend_distance({'close': 100.0, 'breakout_level': 110.0, 'ema': 120.0})
    assert d['need'] == 120.0 and round(d['pct'], 1) == 20.0
    d = DD.trend_distance({'close': 100.0, 'breakout_level': 115.0, 'ema': 90.0})
    assert d['need'] == 115.0
    assert DD.trend_distance({'reason': 'мало дневных свечей'}) is None


def test_digest_red_market_no_positions():
    text = DD.format_digest(_base())
    assert "Сводка за сутки" in text and "13.11 09:00" in text
    assert "v9.9" in text and "2 дн. 3 ч" in text
    assert "🔴 Светофор КРАСНЫЙ" in text and "ниже EMA200" in text and "RSI 27" in text
    assert "155 341 USDT" in text and "просадка 0.1%" in text
    assert "Открытых позиций нет" in text and "сделок не было" in text
    # ближайшая к сигналу монета идёт первой
    assert text.index("BTC 74 014") < text.index("SOL 83.27")
    assert "(+14.0%)" in text and "(+29.1%)" in text
    assert "Проблемы" not in text
    # HTML-разметка Telegram: в тексте нет «голых» угловых скобок, кроме <b>
    assert "<" not in text.replace("<b>", "").replace("</b>", "")


def test_digest_positions_trades_and_warnings():
    d = _base()
    d.update({
        'mode': 'RISK_ONLY', 'auto_trading': False,
        'regime': {'allowed': True, 'label': 'ЗЕЛЕНЫЙ', 'price': 90000.0, 'ema200': 85000.0, 'rsi': 61.0},
        'positions': [{'pair': 'ETHUSDT', 'entry': 2500.0, 'sl': 2300.0, 'strategy': 'TREND', 'pnl_pct': 4.25}],
        'arbitrage_positions': 1,
        'closed_24h': {'count': 2, 'pnl': -12.5, 'wins': 1, 'losses': 1},
        'entries_blocked': '3 убытков подряд — пауза входов до 14.11 09:00',
        'trend': [{'pair': 'BTCUSDT', 'close': 90000.0, 'need': 89000.0, 'pct': -1.1}],
        'problems': ['баланс: timeout'],
    })
    text = DD.format_digest(d)
    assert "режим RISK_ONLY" in text and "авто-торговля ВЫКЛЮЧЕНА" in text
    assert "🟢 Светофор ЗЕЛЕНЫЙ" in text and "выше EMA200" in text
    assert "ETHUSDT [TREND] +4.2%" in text and "стоп 2 300" in text
    assert "Арбитражных позиций: 1" in text
    assert "закрыто сделок: 2" in text and "-12.50 USDT" in text
    assert "Новые входы запрещены" in text
    assert "условие выполнено" in text
    assert "⚠️ Проблемы" in text and "баланс: timeout" in text


def test_digest_with_missing_data_does_not_crash():
    text = DD.format_digest({})
    assert "Сводка за сутки" in text


def test_collect_survives_failing_sources(monkeypatch):
    """Сбой любого источника не ломает сводку — он попадает в «Проблемы»."""
    from bot.services import market_regime_service as mrs, balance_service as bs, trend_strategy as ts

    def boom(*a, **k):
        raise RuntimeError("нет связи")
    monkeypatch.setattr(mrs.market_regime_service, 'get_status', boom)
    monkeypatch.setattr(bs.balance_service, 'get_deposit_usdt', boom)
    monkeypatch.setattr(ts.trend_service, '_daily', boom)

    class Ctl:
        mode, auto_trading_enabled = "ACTIVE", True
        started_at = datetime.now(TIMEZONE) - timedelta(hours=5)

    d = DD.collect(Ctl())
    assert any('светофор' in p for p in d['problems'])
    assert any('баланс' in p for p in d['problems'])
    assert any('тренд' in p for p in d['problems'])
    assert d['positions'] == []            # тестовая БД пустая, источник жив
    text = DD.format_digest(d)
    assert "⚠️ Проблемы" in text and "нет связи" in text


def test_digest_job_registered_and_method_exists():
    from bot.services.job_scheduler import _build_jobs
    from bot.controller import controller
    jobs = {j[0]: j for j in _build_jobs()}
    assert 'daily_digest' in jobs
    assert callable(getattr(controller, jobs['daily_digest'][2]))
