"""
CLI бэктестера.

Примеры:
  python -m bot.backtest                          # 365 дней, пары по умолчанию, walk-forward 4 шага
  python -m bot.backtest --days 540 --folds 5
  python -m bot.backtest --pairs BTCUSDT,ETHUSDT --no-walk-forward
  python -m bot.backtest --require-pullback       # брать только входы с откатом 1h
  python -m bot.backtest --offline                # только кэш, без загрузки

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


def verdict(m: dict, wf: dict | None) -> list:
    """Простые критерии из плана перехода на реальные деньги."""
    src = wf if wf else None
    avg_r = src['oos_avg_r'] if src else m['avg_r']
    pf = src['oos_profit_factor'] if src else m['profit_factor']
    n = src['oos_trades'] if src else m['trades']
    checks = [
        (n >= 50, f"сделок на проверке ≥ 50 (сейчас {n})"),
        (avg_r > 0, f"средний результат после комиссий > 0R (сейчас {avg_r:+.2f}R)"),
        (pf >= 1.2, f"профит-фактор ≥ 1.2 (сейчас {pf:.2f})"),
        (m['max_drawdown'] < cfg.MAX_DRAWDOWN_STOP, f"просадка < {cfg.MAX_DRAWDOWN_STOP * 100:.0f}% "
                                                   f"(сейчас {m['max_drawdown'] * 100:.1f}%)"),
    ]
    lines = ["## Вывод", ""]
    for ok, text in checks:
        lines.append(f"- {'✅' if ok else '❌'} {text}")
    passed = all(ok for ok, _ in checks)
    lines += ["", ("**Критерии выполнены.** Следующий шаг — 4–8 недель на демо с теми же настройками; "
                   "сравнить результаты демо с бэктестом.") if passed else
              ("**Критерии не выполнены.** Преимущество стратегии не подтверждено — на реальные деньги "
               "переходить рано. Смотрите разбивку выше: какие типы входов тянут результат вниз."), ""]
    return lines


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
    ap.add_argument("--out", default=None, help="папка для отчёта (по умолчанию data/backtest/<дата>)")
    args = ap.parse_args(argv)

    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    print(f"🧪 Бэктест: {len(pairs)} пар, {args.days} дней, комиссия {cfg.TRADING_FEE_RATE * 100:.2f}%")
    t0 = time.time()
    data = load_dataset(pairs, args.days, offline=args.offline)
    pairs = [p for p in pairs if data.get(p, {}).get("15")]
    if not pairs or not data.get('BTCUSDT', {}).get('240'):
        print("❌ Нет данных (проверьте сеть или кэш data/klines)")
        return 1

    end_ts = data['BTCUSDT']['15'][-1]['start'] + E.M15
    start_ts = end_ts - args.days * 86_400_000
    kw = dict(require_pullback=args.require_pullback, require_rs=args.require_rs, use_guard=not args.no_guard)

    print("⚙️ Прогон с текущими настройками...")
    res = E.run(data, pairs, start_ts=start_ts, end_ts=end_ts, equity=args.equity, **kw)
    m = E.metrics(res)

    wf = None
    if not args.no_walk_forward and args.folds >= 2:
        print(f"🔁 Walk-forward: {args.folds} шагов, сетка {len(E.DEFAULT_GRID)} вариантов...")
        wf = E.walk_forward(data, pairs, start_ts=start_ts, end_ts=end_ts, folds=args.folds,
                            equity=args.equity, **kw)

    fmt_d = lambda ts: datetime.fromtimestamp(ts / 1000, timezone.utc).strftime('%Y-%m-%d')
    lines = [
        "# Бэктест воронки входа",
        "",
        f"Период: {fmt_d(start_ts)} — {fmt_d(end_ts)} ({args.days} дн.), пары: {', '.join(pairs)}",
        f"Капитал {args.equity:,.0f} USDT, риск на сделку {cfg.MAX_RISK_PER_TRADE * 100:.2f}%, "
        f"комиссия {cfg.TRADING_FEE_RATE * 100:.2f}% + проскальзывание {cfg.SLIPPAGE_ESTIMATE * 100:.2f}%",
        f"Выходы: TP1 {cfg.EXIT_PARTIAL_FRACTION * 100:.0f}% на +{cfg.EXIT_PARTIAL_R}R "
        f"({'вкл' if cfg.EXIT_PARTIAL_ENABLED else 'выкл'}), тейк +{cfg.FUNNEL_TP_R}R, безубыток +{cfg.EXIT_BREAKEVEN_R}R, "
        f"трейлинг {cfg.EXIT_TRAIL_ATR_MULT}×ATR, выход по времени {cfg.EXIT_TIME_HOURS} ч",
        "",
        "> Проверяется ценовая часть воронки (светофор, тренд, откат, триггер, стоп, выходы). "
        "Стакан, поток сделок, OI и funding в истории недоступны — их вклад оценивается на демо.",
        "",
    ]
    lines += _fmt_metrics("Весь период, текущие настройки", m)
    lines += _fmt_breakdown("По наличию отката 1h", E.breakdown(res.trades, lambda t: 'откат был' if t.pullback_ok else 'без отката'))
    lines += _fmt_breakdown("По силе к BTC", E.breakdown(res.trades, lambda t: 'сильнее BTC' if t.rs_ok else 'слабее BTC'))
    lines += _fmt_breakdown("По типу триггера", E.breakdown(res.trades, lambda t: t.trigger or '—'))
    lines += _fmt_breakdown("По парам", E.breakdown(res.trades, lambda t: t.pair))

    if wf:
        lines += ["## Walk-forward (проверка на невиданных данных)", "",
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

    lines += verdict(m, wf)

    out = Path(args.out) if args.out else Path(__file__).resolve().parents[2] / "data" / "backtest" / \
        datetime.now().strftime("%Y-%m-%d_%H%M")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    with (out / "trades.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pair", "opened_utc", "closed_utc", "entry", "stop", "qty", "risk_usd", "pnl", "r",
                    "exit", "trigger", "pullback", "rs", "score"])
        for t in res.trades:
            w.writerow([t.pair, fmt_d(t.opened_ts), fmt_d(t.closed_ts), f"{t.entry:.8g}", f"{t.stop:.8g}",
                        f"{t.qty:.8g}", f"{t.risk_usd:.2f}", f"{t.pnl:.2f}", f"{t.r_multiple:.3f}",
                        t.exit_kind, t.trigger, int(t.pullback_ok), int(t.rs_ok), t.score])
    summary = {'metrics': m, 'walk_forward': {k: v for k, v in (wf or {}).items() if k != 'steps'}}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("\n".join(lines))
    print(f"\n📄 Отчёт: {out / 'report.md'}  ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
