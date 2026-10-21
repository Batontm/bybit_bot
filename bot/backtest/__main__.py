"""
CLI бэктестера.

Примеры:
  python -m bot.backtest                          # 365 дней, пары по умолчанию, walk-forward 4 шага
  python -m bot.backtest --days 540 --folds 5
  python -m bot.backtest --pairs BTCUSDT,ETHUSDT --no-walk-forward
  python -m bot.backtest --require-pullback       # брать только входы с откатом 1h
  python -m bot.backtest --offline                # только кэш, без загрузки
  python -m bot.backtest --strategy trend         # только трендовая стратегия (D1)
  python -m bot.backtest --strategy trend --days 1095 --ext   # + фильтры по данным других бирж

Результаты: data/backtest/<дата>/ — report.md, trades.csv, summary.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import trading_config as cfg
from .data import load_dataset
from . import engine as E

DEFAULT_PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
                 "LINKUSDT", "AVAXUSDT", "LTCUSDT", "DOTUSDT", "TONUSDT", "SUIUSDT"]


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _fmt_metrics(title: str, m: dict) -> list:
    pf = "∞" if m['profit_factor'] == float('inf') else f"{m['profit_factor']:.2f}"
    lines = [
        f"### {title}",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        f"| Сделок | {m['trades']} ({m['trades_per_week']:.1f} в неделю) |",
        f"| Прибыльных | {m['win_rate'] * 100:.0f}% |",
        f"| Средний результат сделки | {m['avg_r']:+.2f} R |",
        f"| Профит-фактор | {pf} |",
        f"| Доходность за период | {_pct(m['total_return'])} |",
        f"| Максимальная просадка | {m['max_drawdown'] * 100:.1f}% |",
        f"| Шарп (дневной, годовой) | {m['sharpe']:.2f} |",
        f"| Стоп по просадке сработал | {'да' if m['halted'] else 'нет'} |",
        "",
        "Выходы: " + ", ".join(f"{k} — {v}" for k, v in sorted(m['by_exit'].items())),
        "",
    ]
    if m['skipped']:
        lines.append("Сигналы, отсеянные после триггера: " +
                     ", ".join(f"{k} — {v}" for k, v in list(m['skipped'].items())[:6]))
        lines.append("")
    return lines


def _fmt_breakdown(title: str, b: dict) -> list:
    lines = [f"**{title}**", "", "| Группа | Сделок | Средний R | Прибыльных |", "|---|---|---|---|"]
    for g, v in sorted(b.items(), key=lambda kv: -kv[1]['trades']):
        lines.append(f"| {g} | {v['trades']} | {v['avg_r']:+.2f} | {v['win_rate'] * 100:.0f}% |")
    lines.append("")
    return lines


def _fmt_wf(wf, fmt_d) -> list:
    if not wf:
        return []
    lines = ["### Walk-forward (проверка на невиданных данных)", "",
             "Параметры подбираются на прошлом, проверяются на следующем отрезке. "
             "Только эти цифры честно оценивают стратегию.", "",
             "| Шаг | Лучшие параметры на train | train: сделок / avgR | test: сделок / avgR |",
             "|---|---|---|---|"]
    for i, s in enumerate(wf['steps'], 1):
        p = ", ".join(f"{k}={v}" for k, v in s['params'].items())
        lines.append(f"| {i} ({fmt_d(s['test'][0])} — {fmt_d(s['test'][1])}) | {p} | "
                     f"{s['train_metrics']['trades']} / {s['train_metrics']['avg_r']:+.2f} | "
                     f"{s['test_metrics']['trades']} / {s['test_metrics']['avg_r']:+.2f} |")
    lines += ["", f"**Итого на test-отрезках:** {wf['oos_trades']} сделок, средний {wf['oos_avg_r']:+.2f}R, "
                  f"прибыльных {wf['oos_win_rate'] * 100:.0f}%, профит-фактор {wf['oos_profit_factor']:.2f}", ""]
    return lines


def verdict(m: dict, wf: dict | None, min_trades: int = 50) -> list:
    """Простые критерии из плана перехода на реальные деньги."""
    src = wf if wf else None
    avg_r = src['oos_avg_r'] if src else m['avg_r']
    pf = src['oos_profit_factor'] if src else m['profit_factor']
    n = src['oos_trades'] if src else m['trades']
    checks = [
        (n >= min_trades, f"сделок на проверке ≥ {min_trades} (сейчас {n})"),
        (avg_r > 0, f"средний результат после комиссий > 0R (сейчас {avg_r:+.2f}R)"),
        (pf >= 1.2, f"профит-фактор ≥ 1.2 (сейчас {pf:.2f})"),
        (m['max_drawdown'] < cfg.MAX_DRAWDOWN_STOP, f"просадка < {cfg.MAX_DRAWDOWN_STOP * 100:.0f}% "
                                                   f"(сейчас {m['max_drawdown'] * 100:.1f}%)"),
    ]
    lines = ["### Вывод", ""]
    for ok, text in checks:
        lines.append(f"- {'✅' if ok else '❌'} {text}")
    passed = all(ok for ok, _ in checks)
    lines += ["", ("**Критерии выполнены.** Следующий шаг — 4–8 недель на демо с теми же настройками; "
                   "сравнить результаты демо с бэктестом.") if passed else
              ("**Критерии не выполнены.** Преимущество стратегии не подтверждено — на реальные деньги "
               "переходить рано. Смотрите разбивку выше: какие типы входов тянут результат вниз."), ""]
    return lines


EXT_VARIANTS = [
    ("без фильтров", {}),
    ("+ funding (лонги не перегреты)", {'TREND_EXT_FUNDING': True}),
    ("+ объём пробоя (Bybit+Binance)", {'TREND_EXT_VOLUME': True}),
    ("+ премия Coinbase", {'TREND_EXT_PREMIUM': True}),
    ("все три", {'TREND_EXT_FUNDING': True, 'TREND_EXT_VOLUME': True, 'TREND_EXT_PREMIUM': True}),
]


def ext_comparison(data, trend_pairs, start_ts, end_ts, args, loader=None):
    """Тренд с фильтрами по данным других бирж: каждый фильтр отдельно и все вместе."""
    from bot.services import trend_strategy as T
    from bot.services import ext_market_data as X
    loader = loader or X.features_for
    print("🌐 Данные других бирж (Binance, Coinbase, funding)...")
    feats, statuses = {}, {}
    for p in trend_pairs:
        days = T.daily_from_4h(data[p]['240'])
        if not days:
            continue
        feats[p], statuses[p] = loader(p, days, days[0]['start'], end_ts)
    lines = ["### Фильтры по данным других бирж", "",
             "Тот же тренд, но пробой пропускается, если фильтр против. Ключи API не нужны — открытые данные.", "",
             "Источники: " + "; ".join(f"{p}: " + ", ".join(f"{k} {v}" for k, v in st.items())
                                       for p, st in statuses.items()), "",
             "| Вариант | Сделок | Средний R | PF | Доходность | Просадка | Проверка: сделок / R / PF | Отсеяно |",
             "|---|---|---|---|---|---|---|---|"]
    off = {'TREND_EXT_FUNDING': False, 'TREND_EXT_VOLUME': False, 'TREND_EXT_PREMIUM': False}
    out = {}
    for name, flags in EXT_VARIANTS:
        with E.override({**off, **flags}):
            r = E.run_trend(data, trend_pairs, start_ts=start_ts, end_ts=end_ts, equity=args.equity,
                            use_guard=not args.no_guard, ext=feats)
            m = E.metrics(r)
            wf = None
            if not args.no_walk_forward and args.folds >= 2:
                wf = E.walk_forward(data, trend_pairs, start_ts=start_ts, end_ts=end_ts, folds=args.folds,
                                    equity=args.equity, grid=E.TREND_GRID, runner=E.run_trend,
                                    min_train_trades=3, use_guard=not args.no_guard, ext=feats, log=lambda *a: None)
        pf = "∞" if m['profit_factor'] == float('inf') else f"{m['profit_factor']:.2f}"
        wf_txt = (f"{wf['oos_trades']} / {wf['oos_avg_r']:+.2f} / {wf['oos_profit_factor']:.2f}" if wf else "—")
        skipped = ", ".join(f"{k} {v}" for k, v in m['skipped'].items()) or "—"
        lines.append(f"| {name} | {m['trades']} | {m['avg_r']:+.2f} | {pf} | {_pct(m['total_return'])} | "
                     f"{m['max_drawdown'] * 100:.1f}% | {wf_txt} | {skipped} |")
        out[name] = {'metrics': m, 'walk_forward': {k: v for k, v in (wf or {}).items() if k != 'steps'}}
    lines += ["", "> Фильтр стоит включать, только если на проверке (walk-forward) средний R и PF выше, чем "
                  "«без фильтров», а сделок осталось хотя бы 15. Иначе это подгонка под историю. "
                  "Включение — в config/trading_config.py: TREND_EXT_FUNDING / TREND_EXT_VOLUME / TREND_EXT_PREMIUM.", ""]
    return lines, out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.backtest", description="Бэктест воронки входа")
    ap.add_argument("--days", type=int, default=365, help="длина периода в днях (по умолчанию 365)")
    ap.add_argument("--pairs", default=",".join(DEFAULT_PAIRS), help="пары через запятую")
    ap.add_argument("--equity", type=float, default=10_000.0, help="стартовый капитал, USDT")
    ap.add_argument("--folds", type=int, default=4, help="шагов walk-forward (по умолчанию 4)")
    ap.add_argument("--no-walk-forward", action="store_true", help="только прогон с текущими настройками")
    ap.add_argument("--require-pullback", action="store_true", help="входить только при откате 1h")
    ap.add_argument("--require-rs", action="store_true", help="входить только если монета сильнее BTC")
    ap.add_argument("--no-guard", action="store_true", help="без паузы после убытков и стопа по просадке")
    ap.add_argument("--offline", action="store_true", help="не качать, использовать только кэш")
    ap.add_argument("--strategy", choices=("funnel", "trend", "both"), default="both",
                    help="какую стратегию проверять (по умолчанию обе)")
    ap.add_argument("--ext", action="store_true",
                    help="тренд: сравнить фильтры по открытым данным Binance/Coinbase (funding, объём, премия)")
    ap.add_argument("--out", default=None, help="папка для отчёта (по умолчанию data/backtest/<дата>)")
    args = ap.parse_args(argv)

    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    do_funnel = args.strategy in ("funnel", "both")
    do_trend = args.strategy in ("trend", "both")
    load_pairs = list(dict.fromkeys((pairs if do_funnel else []) + (list(cfg.TREND_PAIRS) if do_trend else [])))
    print(f"🧪 Бэктест: {args.strategy}, {len(load_pairs)} пар, {args.days} дней, комиссия {cfg.TRADING_FEE_RATE * 100:.2f}%")
    t0 = time.time()
    # тренду нужен длинный прогрев: EMA100 по дневным свечам
    data = load_dataset(load_pairs, args.days, offline=args.offline, warmup_days=160 if do_trend else 60)
    pairs = [p for p in pairs if data.get(p, {}).get("15")]
    trend_pairs = [p for p in cfg.TREND_PAIRS if data.get(p, {}).get("240")]
    if not data.get('BTCUSDT', {}).get('240') or (do_funnel and not pairs and not do_trend):
        print("❌ Нет данных (проверьте сеть или кэш data/klines)")
        return 1

    end_ts = data['BTCUSDT']['15'][-1]['start'] + E.M15
    start_ts = end_ts - args.days * 86_400_000
    kw = dict(require_pullback=args.require_pullback, require_rs=args.require_rs, use_guard=not args.no_guard)
    fmt_d = lambda ts: datetime.fromtimestamp(ts / 1000, timezone.utc).strftime('%Y-%m-%d')

    lines = [
        "# Бэктест стратегий",
        "",
        f"Период: {fmt_d(start_ts)} — {fmt_d(end_ts)} ({args.days} дн.)",
        f"Капитал {args.equity:,.0f} USDT, комиссия {cfg.TRADING_FEE_RATE * 100:.2f}% + проскальзывание "
        f"{cfg.SLIPPAGE_ESTIMATE * 100:.2f}%",
        "",
    ]
    res = None
    m = None
    wf = None
    summary = {}

    if do_funnel and pairs:
        print("⚙️ Воронка: прогон с текущими настройками...")
        res = E.run(data, pairs, start_ts=start_ts, end_ts=end_ts, equity=args.equity, **kw)
        m = E.metrics(res)
        if not args.no_walk_forward and args.folds >= 2:
            print(f"🔁 Воронка: walk-forward {args.folds} шагов, сетка {len(E.DEFAULT_GRID)} вариантов...")
            wf = E.walk_forward(data, pairs, start_ts=start_ts, end_ts=end_ts, folds=args.folds,
                                equity=args.equity, **kw)
        lines += [
            "## Воронка (внутридневная)", "",
            f"Пары: {', '.join(pairs)}. Риск на сделку {cfg.MAX_RISK_PER_TRADE * 100:.2f}%.",
            f"Выходы: TP1 {cfg.EXIT_PARTIAL_FRACTION * 100:.0f}% на +{cfg.EXIT_PARTIAL_R}R "
            f"({'вкл' if cfg.EXIT_PARTIAL_ENABLED else 'выкл'}), тейк +{cfg.FUNNEL_TP_R}R, безубыток +{cfg.EXIT_BREAKEVEN_R}R, "
            f"трейлинг {cfg.EXIT_TRAIL_ATR_MULT}×ATR, выход по времени {cfg.EXIT_TIME_HOURS} ч", "",
            "> Проверяется ценовая часть воронки (светофор, тренд, откат, триггер, стоп, выходы). "
            "Стакан, поток сделок, OI и funding в истории недоступны — их вклад оценивается на демо.", "",
        ]
        lines += _fmt_metrics("Весь период, текущие настройки", m)
        lines += _fmt_breakdown("По наличию отката 1h", E.breakdown(res.trades, lambda t: 'откат был' if t.pullback_ok else 'без отката'))
        lines += _fmt_breakdown("По силе к BTC", E.breakdown(res.trades, lambda t: 'сильнее BTC' if t.rs_ok else 'слабее BTC'))
        lines += _fmt_breakdown("По типу триггера", E.breakdown(res.trades, lambda t: t.trigger or '—'))
        lines += _fmt_breakdown("По парам", E.breakdown(res.trades, lambda t: t.pair))
        lines += _fmt_wf(wf, fmt_d)
        lines += verdict(m, wf)
        summary['funnel'] = {'metrics': m, 'walk_forward': {k: v for k, v in (wf or {}).items() if k != 'steps'}}

    if do_trend and trend_pairs:
        print("🐢 Тренд: прогон...")
        tres = E.run_trend(data, trend_pairs, start_ts=start_ts, end_ts=end_ts, equity=args.equity,
                           use_guard=not args.no_guard)
        tm = E.metrics(tres)
        twf = None
        if not args.no_walk_forward and args.folds >= 2:
            print(f"🔁 Тренд: walk-forward {args.folds} шагов...")
            twf = E.walk_forward(data, trend_pairs, start_ts=start_ts, end_ts=end_ts, folds=args.folds,
                                 equity=args.equity, grid=E.TREND_GRID, runner=E.run_trend,
                                 min_train_trades=3, use_guard=not args.no_guard)
        lines += [
            "## Тренд (дневные свечи)", "",
            f"Пары: {', '.join(trend_pairs)}. Вход — пробой {cfg.TREND_BREAKOUT_DAYS}-дн. максимума выше "
            f"EMA{cfg.TREND_FILTER_EMA}; стоп {cfg.TREND_STOP_ATR}×ATR; выход — трейлинг {cfg.TREND_TRAIL_ATR}×ATR. "
            f"Риск на сделку {cfg.TREND_RISK_PER_TRADE * 100:.2f}%.", "",
            "> Сделок мало, поэтому статистика за год шумная: смотрите и на доходность, и на просадку.", "",
        ]
        lines += _fmt_metrics("Весь период, текущие настройки", tm)
        lines += _fmt_breakdown("По парам", E.breakdown(tres.trades, lambda t: t.pair))
        lines += _fmt_wf(twf, fmt_d)
        lines += verdict(tm, twf, min_trades=15)
        summary['trend'] = {'metrics': tm, 'walk_forward': {k: v for k, v in (twf or {}).items() if k != 'steps'}}
        if args.ext:
            ext_lines, ext_summary = ext_comparison(data, trend_pairs, start_ts, end_ts, args)
            lines += ext_lines
            summary['trend_ext'] = ext_summary
        if res is None:
            res, m = tres, tm

    out = Path(args.out) if args.out else Path(__file__).resolve().parents[2] / "data" / "backtest" / \
        datetime.now().strftime("%Y-%m-%d_%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    with (out / "trades.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pair", "opened_utc", "closed_utc", "entry", "stop", "qty", "risk_usd", "pnl", "r",
                    "exit", "trigger", "pullback", "rs", "score"])
        for t in (res.trades if res else []):
            w.writerow([t.pair, fmt_d(t.opened_ts), fmt_d(t.closed_ts), f"{t.entry:.8g}", f"{t.stop:.8g}",
                        f"{t.qty:.8g}", f"{t.risk_usd:.2f}", f"{t.pnl:.2f}", f"{t.r_multiple:.3f}",
                        t.exit_kind, t.trigger, int(t.pullback_ok), int(t.rs_ok), t.score])
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("\n".join(lines))
    print(f"\n📄 Отчёт: {out / 'report.md'}  ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
