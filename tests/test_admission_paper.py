"""Допуск стратегий (запрещено всё, что не разрешено), отзыв допуска, бумажная торговля."""
import math

import pytest

from config import trading_config as cfg
from bot.db.connection import get_transaction, get_db
from bot.services import admission as A
from bot.services import paper_service as PS
from bot.services import market_state as MS

D1 = MS.D1


@pytest.fixture(autouse=True)
def clean_db():
    def wipe():
        with get_transaction() as conn:
            conn.execute("DELETE FROM paper_trades")
            conn.execute("DELETE FROM settings WHERE key LIKE 'admission_revoked_%' OR key = 'paper_started_at'")
            conn.execute("DELETE FROM positions WHERE pair LIKE 'ADM%'")
    wipe()
    yield
    wipe()


def _table(monkeypatch, **levels):
    table = {k: dict(v) for k, v in cfg.STRATEGY_ADMISSION.items()}
    for name, lv in levels.items():
        table.setdefault(name, {'spec': None, 'title': name})['level'] = lv
    monkeypatch.setattr(cfg, 'STRATEGY_ADMISSION', table)


def _closed(strategy, rs, risk=50.0):
    with get_transaction() as conn:
        for i, r in enumerate(rs):
            conn.execute(
                "INSERT INTO positions (pair, entry_price, quantity, status, opened_at, closed_at, net_pnl, "
                "realized_pnl, strategy, risk_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"ADM{i}USDT", 100.0, 1.0, 'CLOSED', f"2026-01-{i % 28 + 1:02d}T00:00:00",
                 f"2026-02-01T00:{i // 60:02d}:{i % 60:02d}", r * risk, r * risk, strategy, risk))


# ---------------------------------------------------------------- таблица: запрещено по умолчанию

def test_default_table_only_trend_is_live():
    live = [n for n in cfg.STRATEGY_ADMISSION if A.is_live(n)]
    assert live == ['TREND']
    assert A.level('SUPERTREND') == A.PAPER
    for name in ('TREND_SHORT', 'DIP', 'PAIRS', 'FUNNEL', 'RETEST', 'EMA_CROSS'):
        assert A.level(name) == A.OFF


def test_unknown_strategy_is_denied():
    assert A.level('SOMETHING_NEW') == A.OFF and not A.is_live('SOMETHING_NEW')
    assert 'нет в таблице' in A.deny_reason('SOMETHING_NEW')


def test_garbage_level_is_denied(monkeypatch):
    _table(monkeypatch, TREND='YES_PLEASE')
    assert A.level('TREND') == A.OFF


# ---------------------------------------------------------------- строгий критерий

def _trades(rs, start=0):
    return [(start + i, r) for i, r in enumerate(rs)]


def test_evaluate_live_needs_all_conditions():
    rs = [3.0 if i % 3 == 0 else -0.8 for i in range(120)]           # средний ≈ +0.47R
    ev = A.evaluate(_trades(rs), 60)
    assert ev['verdict'] == A.LIVE and ev['p'] < 0.05


def test_evaluate_good_but_few_trades_goes_to_paper():
    rs = [3.0 if i % 3 == 0 else -0.8 for i in range(60)]
    ev = A.evaluate(_trades(rs), 30)
    assert ev['verdict'] == A.PAPER and 'бумаг' in ev['why']


def test_evaluate_rejects_losing_and_lucky():
    assert A.evaluate(_trades([-0.2] * 200), 100)['verdict'] == A.OFF
    # один огромный выигрыш на фоне мелких минусов: средний плюс, но он случаен
    lucky = [-0.3] * 119 + [40.0]
    ev = A.evaluate(_trades(lucky), 60)
    assert ev['verdict'] == A.OFF
    # плюс только в первой половине
    half = [2.0] * 60 + [-0.5] * 60
    ev = A.evaluate(_trades(half), 60)
    assert ev['verdict'] == A.OFF and 'нестабильно' in ev['why']
    assert A.evaluate([], 0)['verdict'] == A.OFF


# ---------------------------------------------------------------- отзыв допуска

def test_r_drawdown_from_peak():
    assert A.r_drawdown([]) == 0
    assert A.r_drawdown([1, 2, -1, -1]) == 2
    assert A.r_drawdown([-1, -1, 5]) == 0


def test_revocation_on_deep_drawdown(monkeypatch):
    monkeypatch.setattr(cfg, 'ADMISSION_REVOKE_DD_R', 12.0)
    _closed('TREND', [3.0, 2.0] + [-1.0] * 11)                        # просадка 11R — ещё терпим
    assert A.check_revocation() == [] and A.is_live('TREND')
    _closed('TREND', [-1.0] * 2)                                      # теперь 13R
    msgs = A.check_revocation()
    assert len(msgs) == 1 and 'отозван' in msgs[0] and '/admission restore TREND' in msgs[0]
    assert A.level('TREND') == A.PAPER and not A.is_live('TREND')
    assert 'отозван' in A.deny_reason('TREND')
    assert A.check_revocation() == []                                 # повторно не шумим
    assert A.restore('TREND') and A.is_live('TREND')
    assert A.restore('TREND') is False


def test_revocation_ignores_other_strategies_and_paper(monkeypatch):
    _closed('FUNNEL', [-1.0] * 30)
    assert A.check_revocation() == [] and A.is_live('TREND')


async def test_trend_entry_blocked_without_admission(monkeypatch):
    from bot.services.trading_service import TradingService
    _table(monkeypatch, TREND='PAPER')
    svc = TradingService.__new__(TradingService)
    pos, err = await svc.open_trend_position('BTCUSDT', 90.0, 5.0)
    assert pos is None and 'не допущена' in err


# ---------------------------------------------------------------- бумажная торговля

def _days(closes, rng=1.0):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({'start': i * D1, 'open': o, 'high': max(o, c) + rng, 'low': min(o, c) - rng,
                    'close': c, 'volume': 1.0})
    return out


FLAT = [100 + (0.2 if i % 2 else -0.2) for i in range(160)]
UPTREND = FLAT + [100 + i * 1.5 for i in range(1, 40)]
ROUND_TRIP = UPTREND + [158 - i * 4 for i in range(1, 15)]


def _paper(monkeypatch, closes, start_day):
    monkeypatch.setattr(cfg, 'PAPER_PAIRS', ['BTCUSDT'])
    with get_transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('paper_started_at', ?)",
                     (str(start_day * D1),))
    return PS.run(fetch_daily=lambda pair: _days(closes))


def _rows(strategy=None):
    with get_db() as conn:
        q = "SELECT * FROM paper_trades" + (" WHERE strategy = ?" if strategy else "") + " ORDER BY opened_ts"
        return [dict(r) for r in conn.execute(q, (strategy,) if strategy else ()).fetchall()]


def test_paper_opens_then_closes_without_duplicates(monkeypatch):
    _table(monkeypatch, TREND='PAPER', SUPERTREND='OFF')
    msgs = _paper(monkeypatch, UPTREND, start_day=150)
    rows = _rows('TREND')
    assert len(rows) == 1 and rows[0]['status'] == 'OPEN' and rows[0]['side'] == 1
    assert rows[0]['state'] in MS.STATES and rows[0]['entry'] > 0 and rows[0]['stop'] > 0
    assert any('Бумага' in m and 'BTCUSDT' in m for m in msgs)

    assert _paper(monkeypatch, UPTREND, start_day=150) == []          # тот же день — ничего нового
    assert len(_rows('TREND')) == 1

    msgs = _paper(monkeypatch, ROUND_TRIP, start_day=150)             # тренд закончился
    rows = _rows('TREND')
    assert len(rows) == 1 and rows[0]['status'] == 'CLOSED' and rows[0]['r'] > 1
    assert any('закрыта' in m for m in msgs)
    st = PS.paper_stats('TREND')
    assert st['closed'] == 1 and st['open'] == 0 and st['avg_r'] == pytest.approx(rows[0]['r'])


def test_paper_ignores_trades_before_start(monkeypatch):
    _table(monkeypatch, TREND='PAPER', SUPERTREND='OFF')
    _paper(monkeypatch, ROUND_TRIP, start_day=len(ROUND_TRIP) + 5)    # запуск после всех сигналов
    assert _rows() == []


def test_paper_skips_off_strategies_and_shadows_live_quietly(monkeypatch):
    _table(monkeypatch, TREND='LIVE', SUPERTREND='OFF', TREND_SHORT='OFF')
    msgs = _paper(monkeypatch, UPTREND, start_day=150)
    assert msgs == []                                                 # «тень» живой стратегии не шумит
    assert {r['strategy'] for r in _rows()} == {'TREND'}              # запрещённые не считаются


def test_closed_paper_trade_is_never_rewritten(monkeypatch):
    _table(monkeypatch, TREND='PAPER', SUPERTREND='OFF')
    _paper(monkeypatch, ROUND_TRIP, start_day=150)
    before = _rows('TREND')[0]
    changed = ROUND_TRIP[:-3] + [x + 30 for x in ROUND_TRIP[-3:]]     # «биржа исправила свечи»
    _paper(monkeypatch, changed, start_day=150)
    after = _rows('TREND')[0]
    assert after['r'] == before['r'] and after['closed_ts'] == before['closed_ts']


# ---------------------------------------------------------------- показ

def test_status_lines(monkeypatch):
    _table(monkeypatch, TREND='PAPER', SUPERTREND='OFF')
    _paper(monkeypatch, ROUND_TRIP, start_day=150)
    _table(monkeypatch, TREND='LIVE', SUPERTREND='PAPER')
    short = "\n".join(A.format_status(A.status()))
    assert "✅ Тренд: пробой вверх — торгует" in short
    assert "📝 Supertrend — бумага; на бумаге 0 из 30 сделок" in short
    assert "⛔ Запрещены:" in short and "Покупка просадки" in short
    full = "\n".join(A.format_status(A.status(), detailed=True))
    assert "убыток достоверен" in full and "⛔ Парная торговля — запрещена" in full
    assert "<" not in full                                           # HTML Telegram


def test_paper_job_registered():
    from bot.services.job_scheduler import _build_jobs
    from bot.controller import controller
    jobs = {j[0]: j for j in _build_jobs()}
    assert 'paper_trading' in jobs and callable(getattr(controller, jobs['paper_trading'][2]))
