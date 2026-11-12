"""
Ежедневная сводка в Telegram: одно сообщение в сутки вместо «всё в порядке»
каждые несколько часов. Заодно служит сигналом «бот жив»: сводка не пришла —
бот остановлен или потерял связь.

format_digest() — чистая функция (тестируется без сети);
collect() — собирает данные из сервисов бота; ошибки отдельных источников
не ломают сводку, а попадают в раздел «Проблемы».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config import trading_config as cfg
from config.settings import TIMEZONE, logger


def _money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def _price(x: float) -> str:
    """Цена без лишних знаков: 71 722, 2 056.4, 83.27, 0.5584."""
    if x >= 1000:
        return f"{x:,.0f}".replace(",", " ")
    if x >= 10:
        return f"{x:,.2f}"
    return f"{x:.4g}"


def _age(delta: timedelta) -> str:
    days, hours = delta.days, delta.seconds // 3600
    if days:
        return f"{days} дн. {hours} ч"
    return f"{hours} ч {delta.seconds % 3600 // 60} мин"


def trend_distance(info: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """
    Сколько осталось до сигнала трендовой стратегии по данным entry_signal():
    дневное закрытие должно быть выше и максимума за N дней, и EMA.
    """
    close, level, ema = info.get('close'), info.get('breakout_level'), info.get('ema')
    if not close or not level or not ema:
        return None
    need = max(level, ema)
    return {'close': close, 'need': need, 'pct': (need / close - 1) * 100}


def format_digest(d: Dict[str, Any]) -> str:
    """Собрать текст сводки. Все ключи необязательны — чего нет, то не выводится."""
    now: datetime = d.get('now') or datetime.now(TIMEZONE)
    lines: List[str] = [f"📋 <b>Сводка за сутки</b> · {now.strftime('%d.%m %H:%M')}"]

    head = f"🤖 Бот {d.get('version', '')} работает".replace("  ", " ")
    if d.get('uptime') is not None:
        head += f" {_age(d['uptime'])} без перезапуска"
    mode = d.get('mode')
    if mode and mode != "ACTIVE":
        head += f" · режим {mode}"
    if d.get('auto_trading') is False:
        head += " · авто-торговля ВЫКЛЮЧЕНА"
    lines.append(head)

    r = d.get('regime')
    if r:
        icon = "🟢" if r.get('allowed') else "🔴"
        line = f"{icon} Светофор {r.get('label', '')}"
        if r.get('price') and r.get('ema200'):
            side = "выше" if r['price'] > r['ema200'] else "ниже"
            gap = abs(r['price'] / r['ema200'] - 1) * 100
            line += f": BTC {_price(r['price'])} {side} EMA200 {_price(r['ema200'])} на {gap:.1f}%"
            if r.get('rsi') is not None:
                line += f", RSI {r['rsi']:.0f}"
        lines.append(line)

    if d.get('equity') is not None:
        line = f"💰 Капитал: {_money(d['equity'])} USDT"
        peak = d.get('equity_peak') or 0
        if peak > 0 and d['equity'] < peak:
            line += f" (пик {_money(peak)}, просадка {(1 - d['equity'] / peak) * 100:.1f}%)"
        lines.append(line)

    if d.get('entries_blocked'):
        lines.append(f"🛑 Новые входы запрещены: {d['entries_blocked']}")

    positions = d.get('positions')
    if positions is not None:
        if not positions:
            lines.append("📂 Открытых позиций нет")
        else:
            lines.append(f"📂 Открытые позиции ({len(positions)}):")
            for p in positions:
                pnl = p.get('pnl_pct')
                pnl_txt = f" {pnl:+.1f}%" if pnl is not None else ""
                sl = f", стоп {_price(p['sl'])}" if p.get('sl') else ""
                strategy = f" [{p['strategy']}]" if p.get('strategy') else ""
                lines.append(f"   • {p['pair']}{strategy}{pnl_txt} (вход {_price(p['entry'])}{sl})")
    if d.get('arbitrage_positions'):
        lines.append(f"⚖️ Арбитражных позиций: {d['arbitrage_positions']}")

    day = d.get('closed_24h')
    if day is not None:
        if day.get('count'):
            lines.append(f"📊 За 24 ч закрыто сделок: {day['count']}, результат {day['pnl']:+.2f} USDT "
                         f"(в плюс {day.get('wins', 0)}, в минус {day.get('losses', 0)})")
        else:
            lines.append("📊 За 24 ч сделок не было")

    trend = d.get('trend')
    if trend:
        lines.append(f"🐢 До покупки по тренду (закрытие дня выше {cfg.TREND_BREAKOUT_DAYS}-дн. максимума "
                     f"и EMA{cfg.TREND_FILTER_EMA}):")
        for t in sorted(trend, key=lambda x: x['pct']):
            base = t['pair'].replace('USDT', '')
            if t['pct'] <= 0:
                lines.append(f"   • {base} {_price(t['close'])} — условие выполнено, вход при закрытии дня")
            else:
                lines.append(f"   • {base} {_price(t['close'])} → нужно выше {_price(t['need'])} (+{t['pct']:.1f}%)")

    problems = d.get('problems') or []
    if problems:
        lines.append("⚠️ Проблемы:")
        lines += [f"   • {p}" for p in problems[:6]]
    return "\n".join(lines)


def collect(controller) -> Dict[str, Any]:
    """Собрать данные для сводки из сервисов бота (сетевые запросы — вызывать в фоновом потоке)."""
    from bot.controller import BOT_VERSION
    now = datetime.now(TIMEZONE)
    d: Dict[str, Any] = {'now': now, 'version': BOT_VERSION, 'problems': []}

    def step(name, fn):
        try:
            fn()
        except Exception as e:
            logger.warning(f"⚠️ Сводка: {name} — {e}")
            d['problems'].append(f"{name}: {str(e)[:80]}")

    def _state():
        d['mode'] = getattr(controller, 'mode', None)
        d['auto_trading'] = getattr(controller, 'auto_trading_enabled', None)
        started = getattr(controller, 'started_at', None)
        if started:
            d['uptime'] = now - started

    def _regime():
        from .market_regime_service import market_regime_service
        st = market_regime_service.get_status(force=True)
        d['regime'] = {'allowed': st.allowed, 'label': st.label, 'price': st.price,
                       'ema200': st.ema200_4h, 'rsi': st.rsi14_4h}
        if st.price is None:
            d['problems'].append(st.reason[:120])

    def _equity():
        from .balance_service import balance_service
        from .account_guard import account_guard
        deposit = balance_service.get_deposit_usdt()
        equity = getattr(balance_service, 'last_total_equity', None) or deposit
        if not equity:
            # сервис баланса при сбое возвращает 0 — «Капитал: 0» в сводке вводил бы в заблуждение
            d['problems'].append("баланс не получен (нет связи с биржей или ключи не приняты)")
        else:
            d['equity'] = equity
        d['equity_peak'] = account_guard.status().get('equity_peak')
        ok, why = account_guard.entries_allowed()
        if not ok:
            d['entries_blocked'] = why

    def _positions():
        from ..db.trades_repo import TradesRepository
        out = []
        for p in TradesRepository.get_open_positions():
            out.append({'pair': p['pair'], 'entry': float(p.get('avg_entry_price') or p['entry_price']),
                        'sl': float(p.get('sl_price') or 0), 'strategy': p.get('strategy'),
                        'pnl_pct': p.get('unrealized_pnl_percent')})
        d['positions'] = out
        end = datetime.now(timezone.utc)
        closed = TradesRepository.get_closed_positions_between_utc(end - timedelta(hours=24), end)
        pnls = [float(c.get('net_pnl') or c.get('realized_pnl') or 0) for c in closed]
        d['closed_24h'] = {'count': len(pnls), 'pnl': sum(pnls),
                           'wins': sum(1 for x in pnls if x > 0), 'losses': sum(1 for x in pnls if x < 0)}

    def _arbitrage():
        from .arbitrage_service import arbitrage_service
        d['arbitrage_positions'] = len(arbitrage_service.repo.get_open_positions())

    def _trend():
        if not cfg.TREND_ENABLED:
            return
        from .trend_strategy import trend_service, entry_signal
        held = {p['pair'] for p in d.get('positions') or []}
        rows = []
        for pair in cfg.TREND_PAIRS:
            if pair in held:
                continue
            days = trend_service._daily(pair)
            if not days:
                continue
            _, info = entry_signal(days)
            dist = trend_distance(info)
            if dist:
                rows.append({'pair': pair, **dist})
        d['trend'] = rows

    step("состояние бота", _state)
    step("светофор", _regime)
    step("баланс", _equity)
    step("позиции", _positions)
    step("арбитраж", _arbitrage)
    step("тренд", _trend)
    return d
