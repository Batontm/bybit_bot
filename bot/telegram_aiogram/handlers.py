from __future__ import annotations

import asyncio
import html
import time
from typing import Optional

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config.api_config import TELEGRAM_ALLOWED_USER_IDS
from config.settings import LOG_FILE
from config.trading_config import ARBITRAGE_POSITION_SIZE_USD, ENTRY_STRATEGY, MAX_TOTAL_RISK
from bot.error_tracker import error_tracker
from bot.db.trades_repo import TradesRepository
from bot.db.daily_pnl_repo import DailyPnLRepository
from bot.controller import BOT_VERSION
from bot.services.balance_service import balance_service
from bot.services.arbitrage_service import arbitrage_service
from bot.services.market_regime_service import market_regime_service


async def _bg(fn, *args):
    """Синхронный код с запросами к бирже — в отдельном потоке, чтобы бот не «замирал»."""
    return await asyncio.to_thread(fn, *args)


async def _exchange(controller, coro_fn, *args):
    """Торговое действие — в поток биржи (по очереди с задачами бота), если он есть."""
    if controller is not None and hasattr(controller, "run_exchange"):
        return await controller.run_exchange(coro_fn, *args)
    return await coro_fn(*args)


def _price(v: float) -> str:
    """Цена с точностью по величине: $67,123.45 / $1.2345 / $0.0001234."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    if v == 0:
        return "$0"
    a = abs(v)
    if a >= 1000:
        return f"${v:,.2f}"
    if a >= 1:
        return f"${v:,.4f}".rstrip('0').rstrip('.')
    return f"${v:.8f}".rstrip('0').rstrip('.') if a < 0.0001 else f"${v:.6f}".rstrip('0').rstrip('.')


def _usd(v: float, sign: bool = False) -> str:
    """Сумма в долларах: -$12.34 / +$5.00 (знак перед $)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    s = "-" if v < 0 else ("+" if sign and v > 0 else "")
    return f"{s}${abs(v):,.2f}"


def _qty(v: float) -> str:
    """Количество монет без лишних нулей."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    if v == 0:
        return "0"
    s = f"{v:,.8f}" if abs(v) < 1 else f"{v:,.4f}"
    return s.rstrip('0').rstrip('.')


def _total_equity_usd(balances: list) -> float:
    """Капитал в USD: totalEquity аккаунта, иначе сумма usdValue по монетам."""
    eq = getattr(balance_service, 'last_total_equity', None)
    if eq:
        return float(eq)
    total = 0.0
    for b in balances:
        v = float(b.get('usd_value') or 0)
        if not v and str(b.get('coin', '')).upper() in ('USDT', 'USDC'):
            v = float(b.get('total') or 0)
        total += v
    return total


def _is_allowed_user_id(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    return user_id in TELEGRAM_ALLOWED_USER_IDS


def _is_allowed_message(message: Message) -> bool:
    user = message.from_user
    return _is_allowed_user_id(user.id if user else None)


def _is_allowed_callback(callback: CallbackQuery) -> bool:
    user = callback.from_user
    return _is_allowed_user_id(user.id if user else None)


def _main_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Дашборд", callback_data="tg:health")
    kb.button(text="🏁 Результаты", callback_data="tg:results")
    kb.button(text="📂 Позиции", callback_data="tg:positions")
    kb.button(text="🧭 Сигналы", callback_data="tg:signals")
    kb.button(text="🚦 Режим", callback_data="tg:regime")
    kb.button(text="📈 PnL", callback_data="tg:pnl")
    kb.button(text="💹 Арбитраж", callback_data="tg:arbitrage")
    kb.button(text="⚙️ Настройки", callback_data="tg:settings")
    kb.button(text="🛠 Сервис", callback_data="tg:service")
    kb.button(text="🛑 PANIC SELL", callback_data="tg:panic_confirm")
    kb.adjust(2, 2, 2, 2, 2)
    return kb


def _settings_kb(controller) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    auto = notif = alerts = scanner = None
    if controller:
        try:
            if hasattr(controller, 'is_auto_trading_enabled'):
                auto = bool(controller.is_auto_trading_enabled())
            if hasattr(controller, 'is_scanner_enabled'):
                scanner = bool(controller.is_scanner_enabled())
            if hasattr(controller, 'is_notifications_enabled'):
                notif = bool(controller.is_notifications_enabled())
            if hasattr(controller, 'is_alerts_enabled'):
                alerts = bool(controller.is_alerts_enabled())
        except Exception:
            pass
    try:
        from bot.services.account_guard import account_guard
        g = account_guard.status()
        if g['drawdown_halt']:
            kb.button(text="▶️ Снять стоп по просадке", callback_data="tg:dd_reset")
        if g['streak_pause_until']:
            kb.button(text="▶️ Снять паузу после серии убытков", callback_data="tg:streak_ack")
    except Exception:
        pass
    mode = controller.get_mode() if controller is not None and hasattr(controller, 'get_mode') else None
    if mode == "PAUSED":
        kb.button(text="▶️ Возобновить работу (сейчас пауза)", callback_data="tg:resume")
    else:
        kb.button(text="⏸ Пауза (сопровождение позиций тоже)", callback_data="tg:pause")
    kb.button(text=f"🤖 Авто-торговля: {'ON' if auto else 'OFF'}", callback_data="tg:toggle_auto_trading")
    if ENTRY_STRATEGY != "FUNNEL":
        kb.button(text=f"🔍 Сканер: {'ON' if scanner else 'OFF'}", callback_data="tg:toggle_scanner")
    kb.button(text=f"🔔 Уведомления: {'ON' if notif else 'OFF'}", callback_data="tg:toggle_notifications")
    kb.button(text=f"🚨 Алерты: {'ON' if alerts else 'OFF'}", callback_data="tg:toggle_alerts")
    kb.button(text="⬅️ Назад", callback_data="tg:menu")
    kb.adjust(1)
    return kb


def _service_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Сверка ордеров", callback_data="tg:reconcile")
    kb.button(text="⚠️ Лог ошибок", callback_data="tg:errors")
    kb.button(text="📋 Логи (tail 50)", callback_data="tg:logs")
    kb.button(text="⬅️ Назад", callback_data="tg:menu")
    kb.adjust(1)
    return kb


def _guard_lines() -> list:
    """Состояние защиты счёта для дашборда и настроек."""
    try:
        from bot.services.account_guard import account_guard
        from config.trading_config import MAX_CONSECUTIVE_LOSSES, MAX_DRAWDOWN_STOP
        g = account_guard.status()
    except Exception:
        return []
    out = []
    if g['drawdown_halt']:
        out.append(f"⛔️ Входы остановлены: просадка ≥ {MAX_DRAWDOWN_STOP * 100:.0f}% от пика {_usd(g['equity_peak'])}")
    if g['streak_pause_until']:
        out.append(f"⏸ Пауза входов после {g['loss_streak']} убытков подряд — до "
                   f"{g['streak_pause_until'].strftime('%d.%m %H:%M')}")
    if not out:
        out.append(f"🛡 Защита счёта: серия убытков {g['loss_streak']}/{MAX_CONSECUTIVE_LOSSES}, "
                   f"стоп при просадке {MAX_DRAWDOWN_STOP * 100:.0f}% от пика {_usd(g['equity_peak'])}")
    return out


def _format_settings_text(controller) -> str:
    auto = notif = alerts = scanner = None
    if controller:
        try:
            if hasattr(controller, 'is_auto_trading_enabled'):
                auto = bool(controller.is_auto_trading_enabled())
            if hasattr(controller, 'is_scanner_enabled'):
                scanner = bool(controller.is_scanner_enabled())
            if hasattr(controller, 'is_notifications_enabled'):
                notif = bool(controller.is_notifications_enabled())
            if hasattr(controller, 'is_alerts_enabled'):
                alerts = bool(controller.is_alerts_enabled())
        except Exception:
            pass

    def _st(v):
        return "✅ ON" if v else "❌ OFF"

    lines = ["⚙️ <b>Настройки</b>"]
    lines.append("")
    try:
        mode = controller.get_mode() if controller is not None else None
        lines.append(f"Режим: <b>{ {'ACTIVE': '🟢 работает', 'RISK_ONLY': '🟡 только сопровождение', 'PAUSED': '🔴 пауза'}.get(mode, mode) }</b>")
    except Exception:
        pass
    lines.append(f"🤖 Авто-торговля: <b>{_st(auto)}</b>")
    lines.extend(_guard_lines())
    if ENTRY_STRATEGY == "FUNNEL":
        lines.append("🧭 Стратегия входа: <b>воронка</b> (пары отбираются автоматически)")
    else:
        lines.append(f"🔍 Сканер Top Gainers: <b>{_st(scanner)}</b>")
    lines.append(f"🔔 Уведомления: <b>{_st(notif)}</b>")
    lines.append(f"🚨 Алерты: <b>{_st(alerts)}</b>")
    return "\n".join(lines)


def _format_arbitrage_pnl_summary() -> str:
    try:
        dash = arbitrage_service.get_dashboard()
        stats = dash.get("stats") or {}
        open_funding = float(stats.get("open_funding") or 0)
        closed_pnl = float(stats.get("total_pnl") or 0)
        total = open_funding + closed_pnl
        open_positions = int(stats.get("open_positions") or 0)
        closed_positions = int(stats.get("closed_positions") or 0)

        lines = ["💹 <b>Арбитраж PnL</b>"]
        lines.append(f"Открыто позиций: {open_positions}")
        lines.append(f"Закрыто позиций: {closed_positions}")
        lines.append("")
        lines.append(f"Funding (открытые): {_usd(open_funding, sign=True)}")
        lines.append(f"Реализовано (закрытые): {_usd(closed_pnl, sign=True)}")
        lines.append(f"Итого: <b>{_usd(total, sign=True)}</b>")
        return "\n".join(lines)
    except Exception as e:
        return f"💹 <b>Арбитраж PnL</b>\n\n❌ Ошибка: {html.escape(str(e))}"


def _format_balances_block(max_rows: int = 5) -> str:
    try:
        balances = balance_service.get_all_balances() or []
    except Exception:
        balances = []

    if not balances:
        return "💰 <b>Баланс (Unified)</b>\nНет данных"

    total_usd = _total_equity_usd(balances)
    usdt = next((b for b in balances if str(b.get('coin', '')).upper() == 'USDT'), {})
    usdt_free = float(usdt.get('free') or 0)

    def usd_of(b):
        v = float(b.get('usd_value') or 0)
        if not v and str(b.get('coin', '')).upper() in ('USDT', 'USDC'):
            v = float(b.get('total') or 0)
        return v

    balances = sorted(balances, key=usd_of, reverse=True)
    visible = [b for b in balances if usd_of(b) >= 1.0]
    dust = [b for b in balances if usd_of(b) < 1.0]

    lines = ["💰 <b>Баланс (Unified)</b>"]
    lines.append(f"├ Всего: <b>{_usd(total_usd)}</b>")
    lines.append(f"└ Свободно USDT: <b>{_usd(usdt_free)}</b>")

    shown = visible[: max(1, int(max_rows))]
    if shown:
        lines.append("")
        for i, b in enumerate(shown):
            coin = str(b.get('coin') or '')
            prefix = "└" if i == len(shown) - 1 and not dust else "├"
            lines.append(f"{prefix} {html.escape(coin)}: <code>{_qty(float(b.get('total') or 0))}</code> ≈ {_usd(usd_of(b))}")
    if dust:
        lines.append(f"└ мелкие остатки (&lt; $1), монет: {len(dust)}")
    return "\n".join(lines)


def _format_open_positions_block(max_rows: int = 10) -> str:
    try:
        positions = TradesRepository.get_open_positions()
    except Exception:
        positions = []

    if not positions:
        return "📂 <b>Активные позиции</b>\nНет открытых"

    lines = [f"📂 <b>Активные позиции ({len(positions)})</b>"]
    for p in positions[: max(1, int(max_rows))]:
        pair = str(p.get('pair') or '')
        coin = pair.replace('USDT', '')
        entry = float(p.get('avg_entry_price') or p.get('entry_price') or 0)
        cur = float(p.get('current_price') or entry or 0)
        qty = float(p.get('quantity') or 0)
        pnl_pct = (cur - entry) / entry * 100 if entry > 0 else None
        pnl_usd = (cur - entry) * qty
        if pnl_pct is not None:
            pnl_emoji = "🟩" if pnl_pct >= 0 else "🟥"
            pnl_txt = f"<code>{pnl_pct:+.2f}%</code> ({_usd(pnl_usd, sign=True)})"
        else:
            pnl_emoji, pnl_txt = "⬜️", "<code>n/a</code>"
        tag = " 🐢" if p.get('strategy') == 'TREND' else ""
        lines.append(f"{pnl_emoji} <b>{html.escape(coin)}</b>{tag}: {pnl_txt}")
        lines.append(f"   {_price(entry)} → {_price(cur)}")
        sl, tp, tp1 = p.get('sl_price'), p.get('tp_price'), p.get('tp1_price')
        parts = []
        if sl:
            parts.append(f"SL {_price(float(sl))}")
        if tp1 and not p.get('partial_taken'):
            parts.append(f"TP1 {_price(float(tp1))}")
        if tp:
            parts.append(f"TP {_price(float(tp))}")
        elif p.get('strategy') == 'TREND':
            parts.append("тейка нет — трейлинг")
        if parts:
            lines.append("   " + " | ".join(parts))

    return "\n".join(lines)


def _format_regime() -> str:
    st = market_regime_service.get_status()
    is_green = bool(st.allowed)
    emoji = "🟢" if is_green else "🔴"
    label = "ЗЕЛЕНЫЙ" if is_green else "КРАСНЫЙ"
    trade_hint = "Торговля разрешена" if is_green else "Торговля заблокирована"

    lines = [f"🚥 <b>Рыночный режим: BTC/USDT (4H)</b>"]
    lines.append(f"Статус: {emoji} <b>{label}</b> ({trade_hint})")

    if st.price is not None and st.ema200_4h is not None and st.rsi14_4h is not None:
        rsi_min = market_regime_service.RSI_MIN
        atr_max = market_regime_service.ATR_SPIKE_MULTIPLIER
        cond_price = st.price > st.ema200_4h
        cond_rsi = st.rsi14_4h >= rsi_min
        atr_ratio = st.atr_ratio
        cond_atr = atr_ratio is None or atr_ratio <= atr_max

        lines.append("")
        lines.append("<b>Показатели:</b>")
        lines.append(
            f"├ Цена: <code>{st.price:,.2f}</code> (нужно &gt; EMA200 {st.ema200_4h:,.2f}) {'✅' if cond_price else '❌'}"
        )
        lines.append(
            f"├ RSI14: <code>{st.rsi14_4h:.2f}</code> (нужно ≥ {rsi_min}) {'✅' if cond_rsi else '❌'}"
        )
        if atr_ratio is not None:
            lines.append(
                f"└ ATR к среднему: <code>{atr_ratio:.2f}×</code> (паника при &gt; {atr_max:.1f}×) {'✅' if cond_atr else '❌'}"
            )
        else:
            lines.append("└ ATR Spike: <code>n/a</code>")
    else:
        lines.append("")
        lines.append(f"⚠️ {html.escape(st.reason or 'Нет данных')}")

    if not is_green and st.reason:
        lines.append("")
        lines.append(f"⛔️ {html.escape(st.reason)}")

    try:
        checked = st.checked_at.strftime("%Y-%m-%d %H:%M:%S")
        lines.append("")
        lines.append(f"<i>Обновлено: {checked}</i>")
    except Exception:
        pass

    return "\n".join(lines)




def _chunk_text(text: str, max_len: int = 3500) -> list[str]:
    if not text:
        return ["(empty)"]
    chunks: list[str] = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + max_len])
        i += max_len
    return chunks


def _format_pre_blocks(text: str, max_len: int = 3500) -> list[str]:
    # Important: we must not chunk an HTML document containing <pre>...</pre>
    # because it may split tags across messages and Telegram will fail parsing.
    escaped = html.escape(text or "")
    return [f"<pre>{c}</pre>" for c in _chunk_text(escaped, max_len=max_len)]


async def _send_pre_blocks(message: Message, blocks: list[str]) -> None:
    # Do NOT use _send_text here: it chunks again and may break <pre> tags.
    for b in blocks:
        await message.answer(b)


async def _send_pre_blocks_cb(callback: CallbackQuery, blocks: list[str]) -> None:
    # Do NOT use _send_text_cb here: it chunks again and may break <pre> tags.
    for b in blocks:
        await callback.message.answer(b)


def _read_last_log_lines(max_lines: int = 80) -> str:
    try:
        if not LOG_FILE.exists():
            return f"LOG_FILE не найден: {LOG_FILE}"

        with LOG_FILE.open("r", encoding="utf-8", errors="ignore") as f:
            lines = [ln for ln in f.readlines() if "| DEBUG" not in ln]
        tail = lines[-max_lines:]
        return "".join(tail).strip()
    except Exception as e:
        return f"Ошибка чтения логов: {e}"


def _format_uptime(controller) -> str:
    try:
        if controller and hasattr(controller, 'started_at'):
            from datetime import datetime
            from config.settings import TIMEZONE
            delta = datetime.now(TIMEZONE) - controller.started_at
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            minutes = rem // 60
            parts = []
            if days:
                parts.append(f"{days}d")
            parts.append(f"{hours}h {minutes}m")
            return " ".join(parts)
    except Exception:
        pass
    return "n/a"


def _format_last_trade_time() -> str:
    try:
        from datetime import datetime as _dt
        from config.settings import TIMEZONE
        from bot.db.connection import get_db
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT closed_at FROM positions WHERE status='CLOSED' AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1"
            )
            row = cursor.fetchone()
        if row and row[0]:
            dt = _dt.fromisoformat(str(row[0]))
            now = _dt.now(TIMEZONE)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TIMEZONE)
            delta = now - dt
            total_min = int(delta.total_seconds() / 60)
            if total_min < 60:
                return f"{total_min} мин. назад"
            elif total_min < 1440:
                return f"{total_min // 60}ч {total_min % 60}м назад"
            else:
                return f"{total_min // 1440}д назад"
    except Exception:
        pass
    return "нет данных"


def _format_health(controller) -> str:
    mode = None
    is_running = None

    try:
        if controller:
            if hasattr(controller, "get_mode"):
                mode = controller.get_mode()
            if hasattr(controller, "is_running"):
                is_running = bool(controller.is_running)
    except Exception:
        pass

    mode_emoji = {"ACTIVE": "🟢", "RISK_ONLY": "🟡", "PAUSED": "🔴"}.get(mode or "", "⚪️")
    uptime = _format_uptime(controller)

    lines = [f"📊 <b>Дашборд системы</b>"]
    lines.append(f"Статус: {mode_emoji} {mode or 'N/A'} | Uptime: <code>{uptime}</code>")
    lines.append(f"Версия: <code>{BOT_VERSION}</code> | Стратегия: <code>{ENTRY_STRATEGY}</code>")
    try:
        st = market_regime_service.get_status()
        light = "🟢 вход разрешён" if st.allowed else "🔴 новые входы запрещены"
        lines.append(f"🚦 Светофор BTC 4H: {light}")
    except Exception:
        pass
    if controller is not None and hasattr(controller, 'is_auto_trading_enabled'):
        try:
            if not controller.is_auto_trading_enabled():
                lines.append("⏸ Авто-торговля выключена (Настройки)")
        except Exception:
            pass
    lines.extend(l for l in _guard_lines() if l.startswith(("⛔", "⏸")))

    lines.append("")
    lines.append(_format_balances_block())

    lines.append("")
    lines.append(_format_open_positions_block())
    try:
        from bot.services.trading_service import TradingService
        positions = TradesRepository.get_open_positions()
        if positions:
            risk_usd = TradingService.open_risk_usdt(positions)
            equity = float(balance_service.get_deposit_usdt() or 0)
            if equity > 0:
                lines.append(f"🛡 Риск до стопов: <code>{_usd(risk_usd)}</code> "
                             f"({risk_usd / equity * 100:.2f}% из {MAX_TOTAL_RISK * 100:.0f}% лимита)")
    except Exception:
        pass

    # арбитраж — компактно
    try:
        dash = arbitrage_service.get_dashboard()
        stats = dash.get('stats') or {}
        arb_positions = dash.get('positions') or []
        open_pos = int(stats.get('open_positions') or 0)
        if open_pos > 0:
            lines.append("")
            lines.append(f"💹 <b>Арбитраж ({open_pos})</b>")
            for ap in arb_positions[:3]:
                pair = str(ap.get('pair') or '')
                coin = pair.replace('USDT', '')
                fund = float(ap.get('accumulated_funding') or 0)
                lines.append(f"• {html.escape(coin)} (арб.): funding <code>{_usd(fund, sign=True)}</code>")
    except Exception:
        pass

    lines.append("")
    lines.append(f"<i>Последний трейд: {_format_last_trade_time()}</i>")

    return "\n".join(lines)


def _format_arbitrage_dashboard() -> str:
    try:
        dash = arbitrage_service.get_dashboard()
        stats = dash.get('stats') or {}
        positions = dash.get('positions') or []

        open_pos = int(stats.get('open_positions') or 0)
        closed_pos = int(stats.get('closed_positions') or 0)
        open_funding = float(stats.get('open_funding') or 0)
        closed_pnl = float(stats.get('total_pnl') or 0)
        total = open_funding + closed_pnl

        lines = ["💹 <b>Арбитраж — Дашборд</b>"]
        lines.append("")
        lines.append("<b>Статистика:</b>")
        lines.append(f"├ Открыто: <code>{open_pos}</code> | Закрыто: <code>{closed_pos}</code>")
        lines.append(f"├ Funding (открытые): <code>{_usd(open_funding, sign=True)}</code>")
        lines.append(f"├ PnL (закрытые): <code>{_usd(closed_pnl, sign=True)}</code>")
        lines.append(f"└ Итого: <b>{_usd(total, sign=True)}</b>")

        if positions:
            lines.append("")
            lines.append(f"📌 <b>Открытые ({len(positions)}):</b>")
            for p in positions[:10]:
                pair = str(p.get('pair') or '')
                coin = pair.replace('USDT', '')
                qty = float(p.get('spot_qty') or 0)
                entry = float(p.get('entry_price') or 0)
                fund = float(p.get('accumulated_funding') or 0)
                fund_emoji = "🟩" if fund >= 0 else "🟥"
                lines.append(f"{fund_emoji} {html.escape(coin)}: <code>{_qty(qty)}</code> @ {_price(entry)} | funding: <code>{_usd(fund, sign=True)}</code>")

        return "\n".join(lines)
    except Exception as e:
        return f"💹 <b>Арбитраж</b>\n\n❌ Ошибка: {html.escape(str(e))}"


def _format_arbitrage_analysis(limit: int = 10, position_size: float = 0) -> tuple[str, list[dict]]:
    """Returns formatted text + opportunities list."""
    try:
        opps = arbitrage_service.scan_funding_rates()
        if not opps:
            return "💹 <b>Арбитраж</b>\n\nНет данных по funding rate.", []

        if position_size <= 0:
            position_size = _get_default_arb_size()

        lines = ["💹 <b>Арбитраж: анализ funding</b>"]
        lines.append(f"Размер позиции: <b>${position_size:.0f}</b> (3% депозита ≈ ${_get_default_arb_size():.0f})")
        min_fr = float(getattr(arbitrage_service, "MIN_FUNDING_RATE", 0.0001))
        lines.append(f"Вход: только BTC/ETH и funding ≥ <code>{min_fr * 100:.4f}%</code> "
                     f"(комиссии полного круга ≈0.31% окупаются ≤ 3 дн.)")
        lines.append("")

        shown = opps[: max(1, int(limit))]
        lines.append("<b>Топ возможностей:</b>")
        for o in shown:
            pair = o.get("pair")
            risk = o.get("risk") or ""
            fr_pct = float(o.get("funding_pct") or 0)
            apy = float(o.get("apy") or 0)
            mark = float(o.get("mark_price") or 0)

            allowed = bool(o.get("entry_ok"))
            can = ("✅ " if allowed else "⛔️ ") + html.escape(str(o.get("reason") or ""))
            lines.append(f"- {html.escape(str(risk))} <b>{html.escape(str(pair))}</b> | funding={fr_pct:.4f}% | APY≈{apy:.1f}% | цена {_price(mark)} | {can}")

        return "\n".join(lines), shown
    except Exception as e:
        return f"💹 <b>Арбитраж</b>\n\n❌ Ошибка анализа: {html.escape(str(e))}", []


# --- Arbitrage position size state ---
_arb_user_size: dict[int, float] = {}  # user_id -> selected size in USD


_arb_default_cache: list = [0.0, 0.0]  # [timestamp, value]


def _get_default_arb_size() -> float:
    """3% депозита (кэш 60 с — раньше каждая кнопка заново запрашивала баланс)."""
    if time.time() - _arb_default_cache[0] < 60 and _arb_default_cache[1] > 0:
        return _arb_default_cache[1]
    value = _calc_default_arb_size()
    _arb_default_cache[0], _arb_default_cache[1] = time.time(), value
    return value


def _calc_default_arb_size() -> float:
    """3% of total deposit, minimum $50, fallback to config value."""
    try:
        grand_total = _total_equity_usd(balance_service.get_all_balances() or [])
        if grand_total > 0:
            size = round(grand_total * 0.03, 2)
            return max(size, 50.0)
    except Exception:
        pass
    return float(ARBITRAGE_POSITION_SIZE_USD)


def _get_arb_size(user_id: int) -> float:
    """Get user's selected arb size or calculate default."""
    return _arb_user_size.get(user_id) or _get_default_arb_size()


def _arb_size_kb(current_size: float) -> InlineKeyboardBuilder:
    """Keyboard with size presets for arbitrage."""
    kb = InlineKeyboardBuilder()
    default = _get_default_arb_size()
    presets = [50, 100, 200, 500]
    # Add 3% default if not already in presets
    if default not in presets:
        presets = [default] + presets
    for s in presets:
        label = f"{'✅ ' if abs(s - current_size) < 0.01 else ''}${s:.0f}"
        if abs(s - _get_default_arb_size()) < 0.01 and s not in [50, 100, 200, 500]:
            label += " (3%)"
        kb.button(text=label, callback_data=f"tg:arb_size:{s:.0f}")
    kb.button(text="🔍 Сканировать", callback_data="tg:arb_scan")
    kb.button(text="⬅️ Дашборд", callback_data="tg:arbitrage")
    kb.adjust(3, 2, 1, 1)
    return kb


def _arbitrage_dashboard_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Размер позиции", callback_data="tg:arb_size_menu")
    kb.button(text="🔍 Сканировать funding", callback_data="tg:arb_scan")
    kb.button(text="🔄 Обновить", callback_data="tg:arbitrage")
    kb.button(text="⬅️ Меню", callback_data="tg:menu")
    kb.adjust(2, 1, 1)
    return kb


def _arbitrage_analysis_kb(opps: list[dict], position_size: float = 0) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    count = 0
    for o in opps:
        if count >= 3:
            break
        pair = o.get("pair")
        if not pair:
            continue
        if not o.get("entry_ok"):
            continue
        kb.button(text=f"▶️ Войти: {pair}", callback_data=f"tg:arb_open:{pair}")
        count += 1
    size_label = f"${position_size:.0f}" if position_size > 0 else f"${_get_default_arb_size():.0f}"
    kb.button(text=f"💰 Размер: {size_label}", callback_data="tg:arb_size_menu")
    kb.button(text="🔄 Обновить скан", callback_data="tg:arb_scan")
    kb.button(text="💹 Дашборд", callback_data="tg:arbitrage")
    kb.button(text="⬅️ Меню", callback_data="tg:menu")
    kb.adjust(1)
    return kb


def _format_pnl_summary(days: int = 7) -> str:
    try:
        if days <= 0:
            rows = DailyPnLRepository.get_last_days(10000)
        else:
            rows = DailyPnLRepository.get_last_days(days)
        today_line = _format_today_pnl()
        if not rows:
            return "📈 <b>PnL</b>\n\n" + (today_line + "\n\n" if today_line else "") + \
                "Итогов по прошлым дням пока нет (считаются раз в сутки, после 00:00 UTC)."

        total_net = sum(float(r.get('net_pnl') or 0) for r in rows)
        total_comm = sum(float(r.get('commission_paid') or 0) for r in rows)
        total_slip = sum(float(r.get('slippage') or 0) for r in rows)
        total_trades = sum(int(r.get('trades_count') or 0) for r in rows)
        total_wins = sum(int(r.get('wins') or 0) for r in rows)
        total_losses = sum(int(r.get('losses') or 0) for r in rows)

        winrate = (total_wins / total_trades * 100) if total_trades > 0 else 0
        avg_pnl = (total_net / total_trades) if total_trades > 0 else 0

        if days <= 0:
            title = "📈 <b>Статистика PnL (всё время)</b>"
        else:
            title = f"📈 <b>Статистика PnL ({len(rows)}д)</b>"

        lines = [title]
        if today_line:
            lines.append(today_line)
            lines.append("")
        lines.append(f"Чистая прибыль: <b>{_usd(total_net, sign=True)}</b>")
        lines.append(f"Винрейт: <code>{winrate:.0f}%</code> | Сделок: <code>{total_trades}</code>")

        lines.append("")
        lines.append("<b>Эффективность:</b>")
        lines.append(f"├ Средний PnL на сделку: <code>{_usd(avg_pnl, sign=True)}</code>")
        lines.append(f"├ Комиссии: <code>-${abs(total_comm):.2f}</code>")
        lines.append(f"└ Проскальзывание (сумма по сделкам): <code>{total_slip * 100:.2f}%</code>")

        lines.append("")
        lines.append("📅 <b>По дням (UTC, последние 15):</b>")
        for r in sorted(rows, key=lambda r: str(r.get('date_utc', '')))[-15:]:
            d = r.get('date_utc', '')
            net = float(r.get('net_pnl') or 0)
            trades = int(r.get('trades_count') or 0)
            day_emoji = "🟩" if net >= 0 else "🟥"
            lines.append(f"{day_emoji} {html.escape(str(d))}: <code>{_usd(net, sign=True)}</code> (сделок: {trades})")

        return "\n".join(lines)
    except Exception as e:
        return f"📈 <b>PnL</b>\n\n❌ Ошибка: {html.escape(str(e))}"


_ERROR_TYPES = {
    'OrderError': 'вход', 'SLError': 'стоп', 'AutoSLError': 'авто-стоп', 'TPSLError': 'тейк/стоп',
    'TPSellError': 'продажа по тейку', 'CloseError': 'закрытие', 'BalanceError': 'баланс',
    'PriceError': 'цена', 'DCAError': 'докупка', 'OpenError': 'арбитраж: вход',
}


def _format_error_lines(items: list) -> list:
    """Время ЧЧ:ММ:СС, что делал бот, суть ошибки и параметры запроса (пара, количество)."""
    import re
    out = []
    for e in items[-15:]:
        ts = str(e.get('timestamp', ''))
        hhmm = ts[11:19] if len(ts) >= 19 else ts
        kind = _ERROR_TYPES.get(e.get('error_type', ''), e.get('error_type', ''))
        msg = str(e.get('message', ''))
        # pybit: «текст (ErrCode: N) (ErrTime: ...). Request → POST url: {params}» — оставляем суть и параметры
        head = re.split(r'\(ErrTime', msg)[0].strip()
        params = re.search(r'\{.*\}', msg)
        detail = ""
        if params:
            keep = re.findall(r"""['"](symbol|side|qty|price|triggerPrice|orderType|orderFilter)['"]\s*:\s*['"]?([^'",}]*)""",
                              params.group(0))
            if keep:
                detail = " — " + ", ".join(f"{k}={v}" for k, v in keep)
        out.append(f"<code>{hhmm}</code> {html.escape(e.get('module', ''))}/{html.escape(kind)}: "
                   f"{html.escape((head + detail)[:300])}")
    return out


def _format_today_pnl() -> str:
    """Закрытые сегодня (UTC) позиции — без ожидания суточного пересчёта."""
    try:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = TradesRepository.get_closed_positions_between_utc(start, now + timedelta(minutes=1))
        if not rows:
            return "📅 Сегодня (UTC): закрытых сделок нет"
        net = sum(float(r.get('net_pnl') or r.get('realized_pnl') or 0) for r in rows)
        wins = sum(1 for r in rows if float(r.get('net_pnl') or r.get('realized_pnl') or 0) > 0)
        return f"📅 Сегодня (UTC): <b>{_usd(net, sign=True)}</b> | сделок {len(rows)}, в плюс {wins}"
    except Exception:
        return ""


# ----------------------------------------------------------------- 🏁 Результаты

def _closed_positions(days: int = 0) -> list:
    from bot.db.connection import get_db
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT pair, strategy, net_pnl, realized_pnl, risk_usd, closed_at, opened_at FROM positions "
            "WHERE status = 'CLOSED' AND closed_at IS NOT NULL ORDER BY closed_at ASC").fetchall()]
    if days > 0:
        from datetime import datetime, timedelta
        from config.settings import TIMEZONE
        cutoff = (datetime.now(TIMEZONE) - timedelta(days=days)).isoformat()
        rows = [r for r in rows if str(r['closed_at']) >= cutoff]
    return rows


def _stats(rows: list, equity: float) -> dict:
    pnls = [float(r['net_pnl'] if r['net_pnl'] not in (None, 0) else (r['realized_pnl'] or 0)) for r in rows]
    rs = [p / float(r['risk_usd']) for p, r in zip(pnls, rows) if r.get('risk_usd')]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), -sum(losses)
    # просадка по закрытым сделкам относительно капитала на начало периода
    start = max(1.0, equity - sum(pnls))
    peak, cur, max_dd = start, start, 0.0
    for p in pnls:
        cur += p
        peak = max(peak, cur)
        max_dd = max(max_dd, 1 - cur / peak)
    streak = 0
    for p in reversed(pnls):
        if p >= 0:
            break
        streak += 1
    return {
        'n': len(pnls), 'pnl': sum(pnls), 'win_rate': len(wins) / len(pnls) if pnls else 0.0,
        'avg_r': sum(rs) / len(rs) if rs else None, 'r_n': len(rs),
        'pf': gw / gl if gl > 0 else (float('inf') if gw > 0 else 0.0),
        'max_dd': max_dd, 'streak': streak,
        'best': max(pnls) if pnls else 0.0, 'worst': min(pnls) if pnls else 0.0,
    }


def _readiness(st: dict):
    """Критерии перехода на реальные деньги (те же, что в отчёте бэктеста)."""
    from config.trading_config import MAX_DRAWDOWN_STOP
    avg_r_txt = "n/a" if st['avg_r'] is None else f"{st['avg_r']:+.2f}R"
    pf_txt = "∞" if st['pf'] == float('inf') else f"{st['pf']:.2f}"
    checks = [
        (st['n'] >= 50, f"сделок ≥ 50 (сейчас {st['n']})"),
        ((st['avg_r'] or 0) > 0, f"средний результат > 0R после комиссий (сейчас {avg_r_txt})"),
        (st['pf'] >= 1.2, f"профит-фактор ≥ 1.2 (сейчас {pf_txt})"),
        (st['max_dd'] < MAX_DRAWDOWN_STOP, f"просадка < {MAX_DRAWDOWN_STOP * 100:.0f}% (сейчас {st['max_dd'] * 100:.1f}%)"),
    ]
    return [f"{'✅' if ok else '❌'} {html.escape(t)}" for ok, t in checks], all(ok for ok, _ in checks)


def _format_results(days: int = 0) -> str:
    rows = _closed_positions(days)
    equity = float(getattr(balance_service, 'last_total_equity', None) or balance_service.get_deposit_usdt() or 0)
    title = "всё время" if days <= 0 else f"{days} дн."
    lines = [f"🏁 <b>Результаты ({title})</b>"]
    if not rows:
        lines.append("")
        lines.append("Закрытых сделок пока нет. Статистика появится после первых закрытий.")
        return "\n".join(lines)

    groups = {'FUNNEL': '🧭 Воронка', 'TREND': '🐢 Тренд'}
    total = _stats(rows, equity)
    for key, label in groups.items():
        part = [r for r in rows if (r.get('strategy') or 'FUNNEL') == key]
        if not part:
            continue
        st = _stats(part, equity)
        pf = '∞' if st['pf'] == float('inf') else f"{st['pf']:.2f}"
        avg_r = 'n/a' if st['avg_r'] is None else f"{st['avg_r']:+.2f}R"
        lines += ["", f"<b>{label}</b>",
                  f"├ Сделок: <code>{st['n']}</code>, прибыльных <code>{st['win_rate'] * 100:.0f}%</code>",
                  f"├ Итог: <b>{_usd(st['pnl'], sign=True)}</b> | средний <code>{avg_r}</code>",
                  f"├ Профит-фактор: <code>{pf}</code>",
                  f"├ Лучшая / худшая: <code>{_usd(st['best'], sign=True)}</code> / <code>{_usd(st['worst'], sign=True)}</code>",
                  f"└ Макс. просадка по сделкам: <code>{st['max_dd'] * 100:.1f}%</code>, убытков подряд сейчас: <code>{st['streak']}</code>"]

    checks, ready = _readiness(total)
    lines += ["", "<b>Готовность к реальным деньгам</b> (по всем сделкам):"] + checks
    lines.append("")
    if ready:
        lines.append("✅ Критерии выполнены. Порядок перехода — в DEPLOY_GUIDE.md, раздел «Переход на реальные деньги».")
    else:
        lines.append("⏳ Пока рано: продолжайте на демо и сравните с бэктестом (<code>python -m bot.backtest</code>).")
    return "\n".join(lines)


def _results_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Всё время", callback_data="tg:results")
    kb.button(text="30 дней", callback_data="tg:results:30")
    kb.button(text="7 дней", callback_data="tg:results:7")
    kb.button(text="⬅️ Меню", callback_data="tg:menu")
    kb.adjust(3, 1)
    return kb


# ----------------------------------------------------------------- 🧭 Сигналы

_COMPONENT_NAMES = {
    'trend_4h': 'тренд', 'pullback_1h': 'откат', 'trigger_15m': 'триггер',
    'relative_strength': 'сила к BTC', 'orderbook': 'стакан', 'aggressor': 'покупки',
    'open_interest': 'OI', 'funding': 'funding', 'crowd': 'толпа',
}


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    from datetime import datetime
    from config.settings import TIMEZONE
    mins = int(max(0, time.time() - ts) // 60)
    hhmm = datetime.fromtimestamp(ts, TIMEZONE).strftime("%H:%M")
    return f"{hhmm} ({'только что' if mins == 0 else f'{mins} мин назад'})"


def _components_txt(comp: dict) -> str:
    parts = [f"{_COMPONENT_NAMES.get(k, k)} {v:+d}" if v < 0 else f"{_COMPONENT_NAMES.get(k, k)} {v}"
             for k, v in comp.items() if v]
    return ", ".join(parts) or "—"


def _format_signals(controller) -> str:
    from config.trading_config import SIGNAL_CHECK_INTERVAL, FUNNEL_MIN_SCORE
    lines = ["🧭 <b>Сигналы — поиск входов</b>"]

    check = getattr(controller, 'last_signal_check', None) if controller is not None else None
    if check:
        lines.append(f"Последняя проверка: {_ago(check.get('at'))} — <b>{html.escape(str(check.get('status', '')))}</b>")
        if check.get('detail'):
            lines.append(f"<i>{html.escape(str(check['detail']))[:300]}</i>")
    else:
        lines.append("Проверок ещё не было (первая — через несколько минут после запуска)")
    lines.append(f"Проверка каждые {SIGNAL_CHECK_INTERVAL // 60} мин | стратегия <code>{ENTRY_STRATEGY}</code>")

    if ENTRY_STRATEGY != "FUNNEL":
        return "\n".join(lines)

    try:
        from bot.services.entry_funnel_service import entry_funnel_service
        scan = entry_funnel_service.last_scan
    except Exception:
        scan = None

    lines.append("")
    if not scan:
        lines.append("Воронка ещё не запускалась (светофор, пауза или риск-лимит не пускают дальше).")
    elif scan.get('error'):
        lines.append(f"⚠️ Последний проход воронки: {_ago(scan.get('at'))} — {html.escape(scan['error'])}")
    else:
        lines.append(f"<b>Последний проход воронки:</b> {_ago(scan.get('at'))}, {scan.get('took', 0):.0f} с")
        lines.append(f"├ Отбор по ликвидности и спреду: <code>{scan.get('universe', 0)}</code>")
        lines.append(f"├ Тренд 4h: <code>{scan.get('trend', 0)}</code>")
        lines.append(f"├ Триггер 15m: <code>{scan.get('trigger', 0)}</code>")
        lines.append(f"└ Балл ≥ {FUNNEL_MIN_SCORE} без вето: <code>{len(scan.get('candidates') or [])}</code>")

        cands = scan.get('candidates') or []
        if cands:
            lines.append("")
            lines.append("🎯 <b>Кандидаты:</b>")
            for c in cands:
                coin = html.escape(c['pair'].replace('USDT', ''))
                risk = ""
                if c.get('stop_pct') is not None and c.get('tp_pct') is not None:
                    risk = f" | SL −{c['stop_pct'] * 100:.2f}% TP +{c['tp_pct'] * 100:.2f}%"
                lines.append(f"• <b>{coin}</b> — {c.get('score')}{risk}")
                lines.append(f"   {html.escape(_components_txt(c.get('components') or {}))}")

        near = scan.get('near_misses') or []
        if near:
            lines.append("")
            lines.append("〰️ <b>Дошли до оценки, но не прошли:</b>")
            for c in near:
                coin = html.escape(c['pair'].replace('USDT', ''))
                why = html.escape("; ".join(c.get('reasons') or [])[:120])
                lines.append(f"• {coin} — {c.get('score')}: {why}")

    try:
        from bot.db.connection import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT created_at, pair, score, decision, reason FROM entry_signals "
                "WHERE stage = 'scored' ORDER BY id DESC LIMIT 6"
            ).fetchall()
        if rows:
            icons = {'OPENED': '✅', 'CANDIDATE': '🎯', 'SKIP': '➖', 'FAILED': '⚠️', 'AI_VETO': '🤖'}
            lines.append("")
            lines.append("🗂 <b>Последние оценки:</b>")
            for r in rows:
                t = str(r['created_at'])[11:16]
                coin = html.escape(str(r['pair']).replace('USDT', ''))
                lines.append(f"{icons.get(r['decision'], '•')} {t} {coin} {r['score']} — "
                             f"{html.escape(str(r['reason'] or ''))[:80]}")
    except Exception:
        pass

    return "\n".join(lines)


def _signals_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data="tg:signals")
    kb.button(text="⬅️ Меню", callback_data="tg:menu")
    kb.adjust(2)
    return kb


# ----------------------------------------------------------------- 📂 Позиции

def _positions_view() -> tuple[str, InlineKeyboardBuilder]:
    text = _format_open_positions_block(max_rows=10)
    kb = InlineKeyboardBuilder()
    try:
        positions = TradesRepository.get_open_positions()
    except Exception:
        positions = []
    for p in positions[:10]:
        coin = str(p.get('pair') or '').replace('USDT', '')
        kb.button(text=f"❌ Закрыть {coin}", callback_data=f"tg:close_ask:{p['id']}")
    kb.button(text="🔄 Обновить", callback_data="tg:positions")
    kb.button(text="⬅️ Меню", callback_data="tg:menu")
    kb.adjust(*([2] * ((len(positions[:10]) + 1) // 2)), 2)
    if positions:
        text += "\n\n<i>Закрытие — по рынку, с отменой TP/SL этой позиции.</i>"
    return text, kb


def _close_confirm_view(position_id: int) -> tuple[str, InlineKeyboardBuilder]:
    kb = InlineKeyboardBuilder()
    p = TradesRepository.get_position_by_id(position_id)
    if not p or p.get('status') != 'OPEN':
        kb.button(text="📂 Позиции", callback_data="tg:positions")
        return "ℹ️ Позиция уже закрыта.", kb
    coin = html.escape(str(p['pair']).replace('USDT', ''))
    entry = float(p.get('avg_entry_price') or p.get('entry_price') or 0)
    cur = float(p.get('current_price') or entry)
    qty = float(p.get('quantity') or 0)
    pnl = (cur - entry) * qty
    pct = (cur - entry) / entry * 100 if entry else 0
    text = (f"❓ <b>Закрыть {coin} по рынку?</b>\n\n"
            f"Количество: <code>{_qty(qty)}</code>\n"
            f"Вход {_price(entry)} → сейчас {_price(cur)}\n"
            f"PnL сейчас: <b>{pct:+.2f}% ({_usd(pnl, sign=True)})</b> без учёта комиссий")
    kb.button(text=f"✅ Да, закрыть {coin}", callback_data=f"tg:close_do:{position_id}")
    kb.button(text="❌ Отмена", callback_data="tg:positions")
    kb.adjust(1)
    return text, kb


def _pnl_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📈 PnL: всё время", callback_data="tg:pnl:all")
    kb.button(text="📈 PnL: 7 дней", callback_data="tg:pnl:7")
    kb.button(text="📈 PnL: 30 дней", callback_data="tg:pnl:30")
    kb.button(text="⬅️ Меню", callback_data="tg:menu")
    kb.adjust(1)
    return kb


async def _send_text(message: Message, text: str, reply_markup=None) -> None:
    for idx, chunk in enumerate(_chunk_text(text)):
        await message.answer(chunk, reply_markup=reply_markup if idx == 0 else None)


async def _send_text_cb(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    # Do not edit existing messages (might be old), just reply with a new one
    for idx, chunk in enumerate(_chunk_text(text)):
        await callback.message.answer(chunk, reply_markup=reply_markup if idx == 0 else None)


def create_router(controller=None) -> Router:
    router = Router()

    @router.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        kb = _main_menu_kb()
        await _send_text(
            message,
            "✅ Telegram интерфейс активен.\n\nВыберите действие в меню ниже или используйте команды.",
            reply_markup=kb.as_markup(),
        )

    @router.message(Command("health"))
    async def cmd_health(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        kb = _main_menu_kb()
        await _send_text(message, await _bg(_format_health, controller), reply_markup=kb.as_markup())

    @router.message(Command("regime"))
    async def cmd_regime(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        kb = _main_menu_kb()
        await _send_text(message, await _bg(_format_regime), reply_markup=kb.as_markup())

    @router.message(Command("errors"))
    async def cmd_errors(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        kb = _main_menu_kb()
        items = error_tracker.get_recent_errors(20)
        if not items:
            await _send_text(message, "✅ Ошибок нет", reply_markup=kb.as_markup())
            return
        lines = ["⚠️ <b>Последние ошибки</b>"]
        lines.extend(_format_error_lines(items))
        await _send_text(message, "\n".join(lines), reply_markup=kb.as_markup())

    @router.message(Command("pnl"))
    async def cmd_pnl(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        await _send_text(message, await _bg(_format_pnl_summary, 7), reply_markup=_pnl_kb().as_markup())

    @router.message(Command("arbitrage"))
    async def cmd_arbitrage(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        await _send_text(message, await _bg(_format_arbitrage_dashboard), reply_markup=_arbitrage_dashboard_kb().as_markup())

    @router.message(Command("reconcile"))
    async def cmd_reconcile(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        kb = _main_menu_kb()
        if not controller or not hasattr(controller, "_reconcile_orphan_orders"):
            await _send_text(message, "❌ Reconcile недоступен (controller не подключён)", reply_markup=kb.as_markup())
            return
        await _send_text(message, "🔁 Запускаю reconciliation...", reply_markup=kb.as_markup())
        try:
            await _exchange(controller, controller._reconcile_orphan_orders)
            await _send_text(message, "✅ Reconcile завершён", reply_markup=kb.as_markup())
        except Exception as e:
            await _send_text(message, f"❌ Ошибка reconcile: {e}", reply_markup=kb.as_markup())

    @router.message(Command("results"))
    async def cmd_results(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        await _send_text(message, await _bg(_format_results, 0), reply_markup=_results_kb().as_markup())

    @router.message(Command("signals"))
    async def cmd_signals(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        await _send_text(message, await _bg(_format_signals, controller), reply_markup=_signals_kb().as_markup())

    @router.message(Command("positions"))
    async def cmd_positions(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        text, pkb = await _bg(_positions_view)
        await _send_text(message, text, reply_markup=pkb.as_markup())

    @router.message(Command("settings"))
    async def cmd_settings(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        await _send_text(
            message,
            _format_settings_text(controller),
            reply_markup=_settings_kb(controller).as_markup(),
        )

    @router.message(Command("service"))
    async def cmd_service(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        await _send_text(
            message,
            "🛠 <b>Сервис</b>\n\nВыберите действие:",
            reply_markup=_service_kb().as_markup(),
        )

    @router.callback_query()
    async def on_callback(callback: CallbackQuery) -> None:
        if not _is_allowed_callback(callback):
            await callback.answer("Forbidden", show_alert=True)
            return

        data = callback.data or ""
        if not data.startswith("tg:"):
            await callback.answer()
            return

        await callback.answer()
        kb = _main_menu_kb().as_markup()

        if data == "tg:health":
            await _send_text_cb(callback, await _bg(_format_health, controller), reply_markup=kb)
        elif data == "tg:regime":
            await _send_text_cb(callback, await _bg(_format_regime), reply_markup=kb)
        elif data == "tg:menu":
            await _send_text_cb(
                callback,
                "✅ Telegram интерфейс активен.\n\nВыберите действие в меню ниже или используйте команды.",
                reply_markup=_main_menu_kb().as_markup(),
            )
        elif data == "tg:errors":
            srv_kb = _service_kb().as_markup()
            items = error_tracker.get_recent_errors(20)
            if not items:
                await _send_text_cb(callback, "✅ Ошибок нет", reply_markup=srv_kb)
                return
            lines = ["⚠️ <b>Последние ошибки ({0})</b>".format(len(items))]
            lines.extend(_format_error_lines(items))
            await _send_text_cb(callback, "\n".join(lines), reply_markup=srv_kb)
        elif data == "tg:reconcile":
            srv_kb = _service_kb().as_markup()
            if not controller or not hasattr(controller, "_reconcile_orphan_orders"):
                await _send_text_cb(callback, "❌ Reconcile недоступен", reply_markup=srv_kb)
                return
            await _send_text_cb(callback, "🔁 Запускаю сверку ордеров...", reply_markup=srv_kb)
            try:
                await _exchange(controller, controller._reconcile_orphan_orders)
                await _send_text_cb(callback, "✅ Сверка завершена", reply_markup=srv_kb)
            except Exception as e:
                await _send_text_cb(callback, f"❌ Ошибка: {e}", reply_markup=srv_kb)
        elif data == "tg:settings":
            await _send_text_cb(
                callback,
                _format_settings_text(controller),
                reply_markup=_settings_kb(controller).as_markup(),
            )
        elif data == "tg:service":
            await _send_text_cb(
                callback,
                "🛠 <b>Сервис</b>\n\nВыберите действие:",
                reply_markup=_service_kb().as_markup(),
            )
        elif data == "tg:logs":
            log_text = _read_last_log_lines(50)
            blocks = _format_pre_blocks(log_text, max_len=3500)
            await _send_pre_blocks_cb(callback, blocks)
            await _send_text_cb(callback, "📋 Логи отправлены", reply_markup=_service_kb().as_markup())
        elif data == "tg:pnl":
            await _send_text_cb(callback, await _bg(_format_pnl_summary, 7), reply_markup=_pnl_kb().as_markup())
        elif data.startswith("tg:pnl:"):
            period = data.split(":", 2)[2] if ":" in data else "7"
            if period == "all":
                days = 0
            else:
                try:
                    days = int(period)
                except Exception:
                    days = 7
            await _send_text_cb(callback, await _bg(_format_pnl_summary, days), reply_markup=_pnl_kb().as_markup())
        elif data == "tg:arbitrage":
            await _send_text_cb(callback, await _bg(_format_arbitrage_dashboard), reply_markup=_arbitrage_dashboard_kb().as_markup())
        elif data == "tg:arb_scan":
            uid = callback.from_user.id if callback.from_user else 0
            def _view():
                size = _get_arb_size(uid)
                text, opps = _format_arbitrage_analysis(10, position_size=size)
                return text, _arbitrage_analysis_kb(opps, position_size=size).as_markup()
            text, markup = await _bg(_view)
            await _send_text_cb(callback, text, reply_markup=markup)
        elif data == "tg:arb_size_menu":
            uid = callback.from_user.id if callback.from_user else 0
            size = await _bg(_get_arb_size, uid)
            default = _get_default_arb_size()
            text = (f"💰 <b>Размер позиции арбитража</b>\n\n"
                    f"Текущий: <b>${size:.0f}</b>\n"
                    f"3% депозита: <code>${default:.0f}</code>\n\n"
                    f"Выберите размер:")
            await _send_text_cb(callback, text, reply_markup=_arb_size_kb(size).as_markup())
        elif data.startswith("tg:arb_size:"):
            uid = callback.from_user.id if callback.from_user else 0
            try:
                new_size = float(data.split(":")[2])
                if new_size < 10:
                    new_size = 10
                _arb_user_size[uid] = new_size
            except Exception:
                pass
            size = await _bg(_get_arb_size, uid)
            default = _get_default_arb_size()
            text = (f"💰 <b>Размер позиции арбитража</b>\n\n"
                    f"✅ Установлен: <b>${size:.0f}</b>\n"
                    f"3% депозита: <code>${default:.0f}</code>\n\n"
                    f"Выберите размер или сканируйте:")
            await _send_text_cb(callback, text, reply_markup=_arb_size_kb(size).as_markup())
        elif data.startswith("tg:arb_open:"):
            pair = data.split(":", 2)[2] if ":" in data else ""
            if not pair:
                await _send_text_cb(callback, "❌ Не удалось определить пару", reply_markup=kb)
                return
            uid = callback.from_user.id if callback.from_user else 0
            size = await _bg(_get_arb_size, uid)
            await _send_text_cb(callback, f"▶️ Открываю арбитраж {html.escape(pair)} на <b>${size:.0f}</b>...", reply_markup=kb)
            try:
                ok, msg = await _exchange(controller, arbitrage_service.open_arbitrage, pair, size)
                prefix = "✅" if ok else "❌"
                await _send_text_cb(callback, f"{prefix} {html.escape(msg)}", reply_markup=kb)
                if ok:
                    await _send_text_cb(callback, await _bg(_format_arbitrage_pnl_summary), reply_markup=kb)
            except Exception as e:
                await _send_text_cb(callback, f"❌ Ошибка открытия арбитража: {html.escape(str(e))}", reply_markup=kb)
        elif data == "tg:toggle_notifications":
            if not controller or not hasattr(controller, "toggle_notifications"):
                await _send_text_cb(callback, "❌ Недоступно", reply_markup=_settings_kb(controller).as_markup())
                return
            try:
                controller.toggle_notifications()
                await _send_text_cb(callback, _format_settings_text(controller), reply_markup=_settings_kb(controller).as_markup())
            except Exception as e:
                await _send_text_cb(callback, f"❌ Ошибка: {e}", reply_markup=_settings_kb(controller).as_markup())
        elif data == "tg:toggle_alerts":
            if not controller or not hasattr(controller, "toggle_alerts"):
                await _send_text_cb(callback, "❌ Недоступно", reply_markup=_settings_kb(controller).as_markup())
                return
            try:
                controller.toggle_alerts()
                await _send_text_cb(callback, _format_settings_text(controller), reply_markup=_settings_kb(controller).as_markup())
            except Exception as e:
                await _send_text_cb(callback, f"❌ Ошибка: {e}", reply_markup=_settings_kb(controller).as_markup())
        elif data == "tg:toggle_auto_trading":
            if not controller or not hasattr(controller, "toggle_auto_trading"):
                await _send_text_cb(callback, "❌ Недоступно", reply_markup=_settings_kb(controller).as_markup())
                return
            try:
                controller.toggle_auto_trading()
                await _send_text_cb(callback, _format_settings_text(controller), reply_markup=_settings_kb(controller).as_markup())
            except Exception as e:
                await _send_text_cb(callback, f"❌ Ошибка: {e}", reply_markup=_settings_kb(controller).as_markup())
        elif data == "tg:toggle_scanner":
            if not controller or not hasattr(controller, "toggle_scanner"):
                await _send_text_cb(callback, "❌ Недоступно", reply_markup=_settings_kb(controller).as_markup())
                return
            try:
                controller.toggle_scanner()
                await _send_text_cb(callback, _format_settings_text(controller), reply_markup=_settings_kb(controller).as_markup())
            except Exception as e:
                await _send_text_cb(callback, f"❌ Ошибка: {e}", reply_markup=_settings_kb(controller).as_markup())
        elif data == "tg:results" or data.startswith("tg:results:"):
            try:
                days = int(data.split(":")[2]) if data.count(":") == 2 else 0
            except Exception:
                days = 0
            await _send_text_cb(callback, await _bg(_format_results, days), reply_markup=_results_kb().as_markup())
        elif data == "tg:signals":
            await _send_text_cb(callback, await _bg(_format_signals, controller), reply_markup=_signals_kb().as_markup())
        elif data == "tg:positions":
            text, pkb = await _bg(_positions_view)
            await _send_text_cb(callback, text, reply_markup=pkb.as_markup())
        elif data.startswith("tg:close_ask:"):
            try:
                pid = int(data.split(":")[2])
            except Exception:
                return
            text, ckb = await _bg(_close_confirm_view, pid)
            await _send_text_cb(callback, text, reply_markup=ckb.as_markup())
        elif data.startswith("tg:close_do:"):
            try:
                pid = int(data.split(":")[2])
            except Exception:
                return
            p = await _bg(TradesRepository.get_position_by_id, pid)
            back = InlineKeyboardBuilder()
            back.button(text="📂 Позиции", callback_data="tg:positions")
            back.button(text="⬅️ Меню", callback_data="tg:menu")
            if not p or p.get('status') != 'OPEN':
                await _send_text_cb(callback, "ℹ️ Позиция уже закрыта.", reply_markup=back.as_markup())
                return
            coin = html.escape(str(p['pair']).replace('USDT', ''))
            await _send_text_cb(callback, f"⏳ Закрываю {coin}...")
            from bot.services.position_service import position_service
            try:
                ok = await _exchange(controller, position_service.close_position, pid, "manual_telegram")
            except Exception:
                ok = False
            after = await _bg(TradesRepository.get_position_by_id, pid)
            if ok and after and after.get('status') == 'CLOSED':
                pnl = float(after.get('net_pnl') or after.get('realized_pnl') or 0)
                text = (f"✅ <b>{coin} закрыта</b> @ {_price(float(after.get('current_price') or 0))}\n"
                        f"PnL: <b>{_usd(pnl, sign=True)}</b> (с оценкой комиссий)")
            else:
                text = (f"⚠️ Не удалось закрыть {coin}. Подробности — в «🛠 Сервис → ⚠️ Лог ошибок». "
                        f"Можно повторить или закрыть вручную на Bybit.")
            await _send_text_cb(callback, text, reply_markup=back.as_markup())
        elif data in ("tg:dd_reset", "tg:streak_ack"):
            from bot.services.account_guard import account_guard
            if data == "tg:dd_reset":
                equity = await _bg(lambda: (balance_service.get_all_balances(),
                                            getattr(balance_service, 'last_total_equity', None))[1])
                account_guard.reset_drawdown(equity)
            else:
                account_guard.ack_loss_streak()
            await _send_text_cb(callback, _format_settings_text(controller), reply_markup=_settings_kb(controller).as_markup())
        elif data in ("tg:pause", "tg:resume"):
            if controller is None or not hasattr(controller, "set_mode"):
                await _send_text_cb(callback, "❌ Недоступно", reply_markup=kb)
                return
            controller.set_mode("PAUSED" if data == "tg:pause" else "ACTIVE")
            await _send_text_cb(callback, _format_settings_text(controller), reply_markup=_settings_kb(controller).as_markup())
        elif data == "tg:panic_confirm":
            confirm_kb = InlineKeyboardBuilder()
            confirm_kb.button(text="⚠️ ДА, ЗАКРЫТЬ ВСЁ", callback_data="tg:panic_execute")
            confirm_kb.button(text="❌ Отмена", callback_data="tg:menu")
            confirm_kb.adjust(1)
            await _send_text_cb(
                callback,
                "🛑 <b>PANIC SELL</b>\n\n"
                "Это действие:\n"
                "• Отменит ВСЕ ордера на бирже\n"
                "• Закроет ВСЕ позиции по рынку\n"
                "• Закроет ВСЕ арбитражные позиции\n"
                "• Поставит бота на ПАУЗУ (снять — Настройки → Возобновить)\n\n"
                "⚠️ <b>Вы уверены?</b>",
                reply_markup=confirm_kb.as_markup(),
            )
        elif data == "tg:panic_execute":
            if not controller or not hasattr(controller, "panic_sell_all"):
                await _send_text_cb(callback, "❌ Panic sell недоступен", reply_markup=kb)
                return
            await _send_text_cb(callback, "🛑 Выполняю PANIC SELL...", reply_markup=kb)
            try:
                # отдельный поток: не ждём очереди торговых задач — паника важнее
                results = await asyncio.to_thread(lambda: asyncio.run(controller.panic_sell_all()))
                await _send_text_cb(callback, "\n".join(results), reply_markup=kb)
            except Exception as e:
                await _send_text_cb(callback, f"❌ Ошибка panic sell: {e}", reply_markup=kb)

    return router
