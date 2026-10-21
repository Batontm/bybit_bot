"""Бэктестер и правила выхода на синтетических данных (без сети)."""
import math
import random

import pytest

from config import trading_config as cfg
from bot.services import exit_rules as X
from bot.backtest import engine as E
from bot.backtest import data as D

M15 = 15 * 60_000


# ----------------------------------------------------------------- синтетика

def synth(seed: int, days: int, start_price: float, drift: float, vol: float, t0: int = 1_700_000_000_000):
    """Случайное блуждание с дрейфом и волнами (тренды + откаты) на 15m, агрегаты 1h/4h."""
    rnd = random.Random(seed)
    n = days * 96
    price = start_price
    m15 = []
    for i in range(n):
        wave = 0.0015 * math.sin(i / 40)
        o = price
        price *= math.exp(drift + wave * 0.1 + rnd.gauss(0, vol))
        hi = max(o, price) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(o, price) * (1 - abs(rnd.gauss(0, vol / 2)))
        m15.append({'start': t0 + i * M15, 'open': o, 'high': hi, 'low': lo, 'close': price,
                    'volume': 100 + rnd.random() * 100 * (2 if rnd.random() < 0.05 else 1), 'turnover': 0})

    def agg(k):
        out = []
        for j in range(0, len(m15) - k + 1, k):
            ch = m15[j:j + k]
            out.append({'start': ch[0]['start'], 'open': ch[0]['open'], 'high': max(c['high'] for c in ch),
                        'low': min(c['low'] for c in ch), 'close': ch[-1]['close'],
                        'volume': sum(c['volume'] for c in ch), 'turnover': 0})
        return out
    return {'15': m15, '60': agg(4), '240': agg(16)}


@pytest.fixture(scope="module")
def dataset():
    return {
        'BTCUSDT': synth(1, 120, 60_000, 0.00012, 0.0025),
        'ALTUSDT': synth(2, 120, 10.0, 0.00015, 0.004),
        'ALT2USDT': synth(3, 120, 2.0, -0.00005, 0.005),
    }


def _period(ds, days):
    end = ds['BTCUSDT']['15'][-1]['start'] + M15
    return end - days * 86_400_000, end


# ----------------------------------------------------------------- exit_rules

def test_breakeven_covers_fees():
    entry = 100.0
    assert X.breakeven_stop(entry) - entry > entry * 2 * cfg.TRADING_FEE_RATE


def test_position_size_includes_costs():
    qty = X.position_size(10_000, 0.005, 100.0, 98.0)
    loss_at_stop = qty * (2.0 + X.cost_per_unit(100.0))
    assert loss_at_stop == pytest.approx(50.0)


def _pos(entry=100.0, stop=98.0, qty=10.0, atr=1.0):
    return X.SimPosition(pair='X', entry=entry, qty=qty, lv=X.levels(entry, stop), atr=atr, opened_ts=0)


def bar(o, h, l, c, i=1):
    return {'start': i * M15, 'open': o, 'high': h, 'low': l, 'close': c}


def test_sim_stop_first_when_ambiguous():
    p = _pos()
    assert X.simulate_bar(p, bar(100, 106, 97, 100))    # и стоп, и тейк в одной свече
    assert p.fills[-1][0] == 'stop'
    assert X.realized_pnl(p) < 0


def test_sim_partial_then_trailing_then_stop():
    p = _pos()
    # +1.5R (103) → TP1 половины, стоп в безубыток
    assert not X.simulate_bar(p, bar(100, 103.2, 99.5, 103))
    assert p.partial_done and p.remaining == pytest.approx(5.0)
    # стоп не ниже безубытка; в той же свече трейлинг: 103 − 2×1 = 101
    assert p.stop == pytest.approx(max(X.breakeven_stop(100), 101.0))
    # рост → трейлинг поднимает стоп: 104.5 − 2×1 = 102.5
    assert not X.simulate_bar(p, bar(103, 104.6, 102.8, 104.5, i=2))
    assert p.stop == pytest.approx(102.5)
    # откат → стоп по трейлингу
    assert X.simulate_bar(p, bar(104, 104.2, 102.0, 102.2, i=3))
    assert X.realized_pnl(p) > 0


def test_sim_full_target():
    p = _pos()
    X.simulate_bar(p, bar(100, 103.1, 99.8, 103))
    assert X.simulate_bar(p, bar(103, 105.2, 102.9, 105, i=2))    # тейк 2.5R = 105
    kinds = [f[0] for f in p.fills]
    assert kinds == ['tp1', 'tp']


def test_sim_time_exit():
    p = _pos()
    hours = cfg.EXIT_TIME_HOURS
    done = False
    for i in range(1, hours * 4 + 2):
        done = X.simulate_bar(p, bar(100, 100.4, 99.6, 100.1, i=i))
        if done:
            break
    assert done and p.fills[-1][0] == 'time'


# ----------------------------------------------------------------- движок

def test_run_produces_consistent_accounting(dataset):
    start, end = _period(dataset, 90)
    res = E.run(dataset, ['ALTUSDT', 'ALT2USDT'], start_ts=start, end_ts=end, equity=10_000)
    m = E.metrics(res)
    assert res.signals_seen > 0
    assert m['trades'] == len(res.trades)
    # капитал в конце = старт + сумма PnL сделок
    assert res.start_equity + sum(t.pnl for t in res.trades) == pytest.approx(
        res.start_equity * (1 + m['total_return']))
    for t in res.trades:
        assert t.risk_usd > 0
        assert t.r_multiple >= -1.6          # стоп + проскальзывание/гэп — не катастрофа
        assert t.closed_ts >= t.opened_ts
    assert 0 <= m['max_drawdown'] < 1


def test_regime_off_gives_more_signals(dataset):
    start, end = _period(dataset, 60)
    on = E.run(dataset, ['ALTUSDT'], start_ts=start, end_ts=end)
    off = E.run(dataset, ['ALTUSDT'], start_ts=start, end_ts=end, use_regime=False)
    assert off.signals_seen >= on.signals_seen


def test_override_restores_config():
    before = cfg.FUNNEL_TP_R
    with E.override({'FUNNEL_TP_R': 9.0}):
        assert cfg.FUNNEL_TP_R == 9.0
    assert cfg.FUNNEL_TP_R == before


def test_walk_forward_runs(dataset):
    start, end = _period(dataset, 90)
    grid = [{'FUNNEL_TP_R': 2.0}, {'FUNNEL_TP_R': 3.0}]
    wf = E.walk_forward(dataset, ['ALTUSDT', 'ALT2USDT'], start_ts=start, end_ts=end, folds=2,
                        grid=grid, min_train_trades=1, log=lambda *a: None)
    assert len(wf['steps']) == 2
    assert wf['oos_trades'] == sum(s['test_metrics']['trades'] for s in wf['steps'])
    assert cfg.FUNNEL_TP_R == 2.5           # сетка не «протекла» в конфиг


def test_cli_offline_report(dataset, tmp_path, monkeypatch):
    from bot.backtest import __main__ as cli
    monkeypatch.setattr(cli, 'load_dataset', lambda pairs, days, offline=False, **kw: dataset)
    out = tmp_path / "bt"
    code = cli.main(["--days", "60", "--pairs", "ALTUSDT,ALT2USDT", "--folds", "2", "--out", str(out),
                     "--strategy", "funnel"])
    assert code == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "Walk-forward" in report and "### Вывод" in report
    assert (out / "trades.csv").exists() and (out / "summary.json").exists()


# ----------------------------------------------------------------- загрузка истории

class _PagingClient:
    """Bybit отдаёт ≤1000 свечей от новых к старым в пределах [start, end]."""

    def __init__(self, first, n, step):
        self.all = [[str(first + i * step), '1', '2', '0.5', '1.5', '10', '15'] for i in range(n)]
        self.calls = 0

    def get_kline(self, category, symbol, interval, start, end, limit):
        self.calls += 1
        rows = [r for r in self.all if start <= int(r[0]) <= end]
        return {'retCode': 0, 'result': {'list': list(reversed(rows))[:limit]}}


def test_fetch_klines_paginates_and_caches(tmp_path):
    step = D.INTERVAL_MS['60']
    first = 1_600_000_000_000 - (1_600_000_000_000 % step)
    client = _PagingClient(first, 2500, step)
    rows = D.fetch_klines('XUSDT', '60', first, first + 2500 * step, client=client, cache_dir=tmp_path,
                          log=lambda *a: None)
    assert len(rows) == 2500 and rows[0]['start'] == first and client.calls >= 3
    # второй вызов — из кэша, без запросов
    calls = client.calls
    again = D.fetch_klines('XUSDT', '60', first, first + 2500 * step, client=client, cache_dir=tmp_path,
                           log=lambda *a: None)
    assert len(again) == 2500 and client.calls == calls


# ----------------------------------------------------------------- тренд

def test_trend_rules_on_synthetic_uptrend():
    from bot.services import trend_strategy as T
    days = []
    price = 100.0
    for i in range(200):
        o = price
        price *= 1.004 if i < 180 else 1.03          # в конце — рывок вверх (пробой)
        days.append({'start': i * T.D1, 'open': o, 'high': price * 1.01, 'low': o * 0.99, 'close': price,
                     'volume': 1})
    ok, info = T.entry_signal(days)
    assert ok and info['stop'] < info['close'] and info['close'] > info['breakout_level']
    assert T.chandelier_stop(info['stop'], info['close'] * 1.2, info['atr']) > info['stop']
    assert T.chandelier_stop(info['stop'], info['close'] * 0.5, info['atr']) == info['stop']   # только вверх


def test_daily_from_4h_skips_partial_days():
    from bot.services import trend_strategy as T
    c = [{'start': i * 4 * 3_600_000, 'open': 1, 'high': 2 + i, 'low': 0.5, 'close': 1.5, 'volume': 1}
         for i in range(14)]          # 2 полных дня + 2 свечи третьего
    d = T.daily_from_4h(c)
    assert len(d) == 2 and d[0]['high'] == 7 and d[1]['high'] == 13


def test_run_trend_backtest(dataset):
    start, end = _period(dataset, 60)
    with E.override({'TREND_FILTER_EMA': 20, 'TREND_BREAKOUT_DAYS': 5}):
        res = E.run_trend(dataset, ['BTCUSDT', 'ALTUSDT'], start_ts=start, end_ts=end)
    m = E.metrics(res)
    assert m['trades'] == len(res.trades)
    for t in res.trades:
        assert t.trigger == 'breakout' and t.risk_usd > 0 and t.r_multiple > -1.5


def test_cli_trend_only(dataset, tmp_path, monkeypatch):
    from bot.backtest import __main__ as cli
    monkeypatch.setattr(cli, 'load_dataset', lambda pairs, days, offline=False, **kw: dataset)
    monkeypatch.setattr(cli.cfg, 'TREND_PAIRS', ['BTCUSDT', 'ALTUSDT'])
    out = tmp_path / "tr"
    assert cli.main(["--days", "60", "--strategy", "trend", "--folds", "2", "--out", str(out)]) == 0
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## Тренд" in report and "### Вывод" in report
