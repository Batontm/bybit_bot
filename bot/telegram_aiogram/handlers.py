from __future__ import annotations

import html
from typing import Optional

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config.api_config import TELEGRAM_ALLOWED_USER_IDS
from config.settings import LOG_FILE
from config.trading_config import ARBITRAGE_POSITION_SIZE_USD
from bot.error_tracker import error_tracker
from bot.db.trades_repo import TradesRepository
from bot.db.daily_pnl_repo import DailyPnLRepository
from bot.controller import BOT_VERSION
from bot.services.balance_service import balance_service
from bot.services.arbitrage_service import arbitrage_service
from bot.services.market_regime_service import market_regime_service


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
    kb.button(text="� Дашборд", callback_data="tg:health")
    kb.button(text="🚦 Режим", callback_data="tg:regime")
    kb.button(text=" PnL", callback_data="tg:pnl")
    kb.button(text="💹 Арбитраж", callback_data="tg:arbitrage")
    kb.button(text="⚙️ Настройки", callback_data="tg:settings")
    kb.button(text="� Сервис", callback_data="tg:service")
    kb.button(text="🛑 PANIC SELL", callback_data="tg:panic_confirm")
    kb.adjust(2, 2, 2, 1)
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
    kb.button(text=f"🤖 Авто-торговля: {'ON' if auto else 'OFF'}", callback_data="tg:toggle_auto_trading")
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
    lines.append(f"🤖 Авто-торговля: <b>{_st(auto)}</b>")
    lines.append(f"🔍 Сканер: <b>{_st(scanner)}</b>")
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
        lines.append(f"Funding (открытые): ${open_funding:.2f}")
        lines.append(f"Реализовано (закрытые): ${closed_pnl:.2f}")
        lines.append(f"Итого: <b>${total:.2f}</b>")
        return "\n".join(lines)
    except Exception as e:
        return f"💹 <b>Арбитраж PnL</b>\n\n❌ Ошибка: {html.escape(str(e))}"


def _format_market_preview() -> str:
    st = market_regime_service.get_status()
    if st.price is None or st.ema200_4h is None or st.rsi14_4h is None:
        return "Рынок (BTC 4H): неизвестно"

    above = st.price > st.ema200_4h
    rsi_up = st.rsi14_4h >= 50

    if above and rsi_up:
        label = "БЫЧИЙ"
        emoji = "🟢"
    elif (not above) and (not rsi_up):
        label = "МЕДВЕЖИЙ"
        emoji = "🔴"
    else:
        label = "ФЛЭТ/СМЕШАННЫЙ"
        emoji = "🟡"

    return (
        f"{emoji} Рынок (BTC 4H): <b>{label}</b> | Цена={st.price:.2f} | EMA200={st.ema200_4h:.2f} | RSI14={st.rsi14_4h:.2f}"
    )


def _format_balances_block(max_rows: int = 5) -> str:
    try:
        balances = balance_service.get_all_balances() or []
    except Exception:
        balances = []

    if not balances:
        return "💰 <b>Баланс (Unified)</b>\nНет данных"

    try:
        balances = sorted(balances, key=lambda b: float(b.get('total') or 0), reverse=True)
    except Exception:
        pass

    grand_total = sum(float(b.get('usd_value') or b.get('total') or 0) for b in balances)
    grand_free = sum(float(b.get('free') or 0) for b in balances if str(b.get('coin', '')).upper() == 'USDT')

    lines = ["💰 <b>Баланс (Unified)</b>"]
    lines.append(f"├ Total: <b>${grand_total:,.2f}</b>")
    lines.append(f"└ Available: <b>${grand_free:,.2f}</b> (USDT)")

    shown = balances[: max(1, int(max_rows))]
    if len(shown) > 0:
        lines.append("")
        for i, b in enumerate(shown):
            coin = str(b.get('coin') or '')
            total = float(b.get('total') or 0)
            free = float(b.get('free') or 0)
            prefix = "└" if i == len(shown) - 1 else "├"
            lines.append(f"{prefix} {html.escape(coin)}: <code>{total:.4f}</code> (free: {free:.4f})")
    return "\n".join(lines)


def _format_open_positions_block(max_rows: int = 10) -> str:
    try:
        positions = TradesRepository.get_open_positions()
    except Exception:
        positions = []

    if not positions:
        return "� <b>Активные позиции</b>\nНет открытых"

    lines = [f"� <b>Активные позиции ({len(positions)})</b>"]
    for p in positions[: max(1, int(max_rows))]:
        pair = str(p.get('pair') or '')
        coin = pair.replace('USDT', '')
        cur = float(p.get('current_price') or p.get('entry_price') or 0)
        pnl_pct = p.get('unrealized_pnl_percent')
        try:
            pnl_pct_f = float(pnl_pct) if pnl_pct is not None else None
        except Exception:
            pnl_pct_f = None
        if pnl_pct_f is not None:
            pnl_emoji = "🟩" if pnl_pct_f >= 0 else "🟥"
            pnl_sign = "+" if pnl_pct_f >= 0 else ""
            pnl_txt = f"<code>{pnl_sign}{pnl_pct_f:.2f}%</code>"
        else:
            pnl_emoji = "⬜️"
            pnl_txt = "<code>n/a</code>"
        lines.append(f"{pnl_emoji} {html.escape(coin)}: {pnl_txt} | ${cur:,.2f}")

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
        cond_price = st.price > st.ema200_4h
        cond_rsi = st.rsi14_4h >= 50
        atr_ratio = st.atr_ratio
        cond_atr = (atr_ratio is not None and atr_ratio < 2.0) if atr_ratio is not None else True

        lines.append("")
        lines.append("<b>Показатели:</b>")
        lines.append(
            f"├ Цена: <code>{st.price:,.2f}</code> (Target &gt; {st.ema200_4h:,.2f}) {'✅' if cond_price else '❌'}"
        )
        lines.append(
            f"├ RSI14: <code>{st.rsi14_4h:.2f}</code> (Target &gt; 50) {'✅' if cond_rsi else '❌'}"
        )
        if atr_ratio is not None:
            lines.append(
                f"└ ATR Spike: <code>{atr_ratio:.2f}</code> (Limit &lt; 2.0) {'✅' if cond_atr else '❌'}"
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
            lines = f.readlines()
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
    lines.append(f"Версия: <code>{BOT_VERSION}</code>")

    lines.append("")
    lines.append(_format_balances_block())

    lines.append("")
    lines.append(_format_open_positions_block())

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
                lines.append(f"• {html.escape(coin)}(Arb): <code>+${fund:.2f} funding</code>")
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

        total_sign = "+" if total >= 0 else ""
        lines = ["💹 <b>Арбитраж — Дашборд</b>"]
        lines.append("")
        lines.append("<b>Статистика:</b>")
        lines.append(f"├ Открыто: <code>{open_pos}</code> | Закрыто: <code>{closed_pos}</code>")
        lines.append(f"├ Funding (open): <code>+${open_funding:.2f}</code>")
        lines.append(f"├ PnL (closed): <code>${closed_pnl:.2f}</code>")
        lines.append(f"└ Итого: <b>{total_sign}${total:.2f}</b>")

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
                lines.append(f"{fund_emoji} {html.escape(coin)}: <code>{qty:.4f}</code> @ ${entry:.4f} | funding: <code>+${fund:.2f}</code>")

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
        lines.append("")

        shown = opps[: max(1, int(limit))]
        lines.append("<b>Топ возможностей:</b>")
        for o in shown:
            pair = o.get("pair")
            risk = o.get("risk") or ""
            fr_pct = float(o.get("funding_pct") or 0)
            apy = float(o.get("apy") or 0)
            mark = float(o.get("mark_price") or 0)

            # Разрешаем вход только если funding не ниже MIN_FUNDING_RATE сервиса
            allowed = float(o.get("funding_rate") or 0) >= float(getattr(arbitrage_service, "MIN_FUNDING_RATE", 0.0001))
            can = "✅ можно" if allowed else "⛔️ нельзя"
            lines.append(f"- {risk} <b>{pair}</b> | funding={fr_pct:.4f}% | APY≈{apy:.1f}% | price={mark:.4f} | {can}")

        return "\n".join(lines), shown
    except Exception as e:
        return f"💹 <b>Арбитраж</b>\n\n❌ Ошибка анализа: {html.escape(str(e))}", []


# --- Arbitrage position size state ---
_arb_user_size: dict[int, float] = {}  # user_id -> selected size in USD


def _get_default_arb_size() -> float:
    """3% of total deposit, minimum $50, fallback to config value."""
    try:
        balances = balance_service.get_all_balances() or []
        grand_total = sum(float(b.get('usd_value') or b.get('total') or 0) for b in balances)
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
        allowed = float(o.get("funding_rate") or 0) >= float(getattr(arbitrage_service, "MIN_FUNDING_RATE", 0.0001))
        if not allowed:
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
        if not rows:
            return "📈 <b>PnL</b>\n\nНет данных (пока не было закрытых позиций)."

        total_net = sum(float(r.get('net_pnl') or 0) for r in rows)
        total_comm = sum(float(r.get('commission_paid') or 0) for r in rows)
        total_slip = sum(float(r.get('slippage') or 0) for r in rows)
        total_trades = sum(int(r.get('trades_count') or 0) for r in rows)
        total_wins = sum(int(r.get('wins') or 0) for r in rows)
        total_losses = sum(int(r.get('losses') or 0) for r in rows)

        winrate = (total_wins / total_trades * 100) if total_trades > 0 else 0
        avg_pnl = (total_net / total_trades) if total_trades > 0 else 0
        net_sign = "+" if total_net >= 0 else ""

        if days <= 0:
            title = "📈 <b>Статистика PnL (всё время)</b>"
        else:
            title = f"📈 <b>Статистика PnL ({len(rows)}д)</b>"

        lines = [title]
        lines.append(f"Net Profit: <b>{net_sign}${total_net:.2f}</b>")
        lines.append(f"Winrate: <code>{winrate:.0f}%</code> | Trades: <code>{total_trades}</code>")

        lines.append("")
        lines.append("<b>Эффективность:</b>")
        lines.append(f"├ Avg PnL/trade: <code>${avg_pnl:.2f}</code>")
        lines.append(f"├ Fees: <code>-${abs(total_comm):.2f}</code>")
        lines.append(f"└ Slippage: <code>-${abs(total_slip):.2f}</code>")

        lines.append("")
        lines.append("📅 <b>Daily:</b>")
        for r in rows[-15:]:
            d = r.get('date_utc', '')
            net = float(r.get('net_pnl') or 0)
            trades = int(r.get('trades_count') or 0)
            day_emoji = "🟩" if net >= 0 else "🟥"
            net_s = "+" if net >= 0 else ""
            lines.append(f"{day_emoji} {d}: <code>{net_s}${net:.2f}</code> ({trades} сделок)")

        return "\n".join(lines)
    except Exception as e:
        return f"📈 <b>PnL</b>\n\n❌ Ошибка: {e}"


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
        await _send_text(message, _format_health(controller), reply_markup=kb.as_markup())

    @router.message(Command("regime"))
    async def cmd_regime(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        kb = _main_menu_kb()
        await _send_text(message, _format_regime(), reply_markup=kb.as_markup())

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
        for e in items:
            lines.append(
                html.escape(
                    f"{e.get('timestamp','')} | {e.get('module','')} | {e.get('error_type','')} | {e.get('message','')}"
                )
            )
        await _send_text(message, "\n".join(lines), reply_markup=kb.as_markup())

    @router.message(Command("pnl"))
    async def cmd_pnl(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        await _send_text(message, _format_pnl_summary(7), reply_markup=_pnl_kb().as_markup())

    @router.message(Command("arbitrage"))
    async def cmd_arbitrage(message: Message) -> None:
        if not _is_allowed_message(message):
            return
        await _send_text(message, _format_arbitrage_dashboard(), reply_markup=_arbitrage_dashboard_kb().as_markup())

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
            await controller._reconcile_orphan_orders()
            await _send_text(message, "✅ Reconcile завершён", reply_markup=kb.as_markup())
        except Exception as e:
            await _send_text(message, f"❌ Ошибка reconcile: {e}", reply_markup=kb.as_markup())

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
            "� <b>Сервис</b>\n\nВыберите действие:",
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
            await _send_text_cb(callback, _format_health(controller), reply_markup=kb)
        elif data == "tg:regime":
            await _send_text_cb(callback, _format_regime(), reply_markup=kb)
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
            for e in items:
                ts = str(e.get('timestamp', ''))[-8:]
                mod = e.get('module', '')
                msg = e.get('message', '')
                lines.append(f"<code>{ts}</code> | {html.escape(mod)} | {html.escape(msg[:80])}")
            await _send_text_cb(callback, "\n".join(lines), reply_markup=srv_kb)
        elif data == "tg:reconcile":
            srv_kb = _service_kb().as_markup()
            if not controller or not hasattr(controller, "_reconcile_orphan_orders"):
                await _send_text_cb(callback, "❌ Reconcile недоступен", reply_markup=srv_kb)
                return
            await _send_text_cb(callback, "🔁 Запускаю сверку ордеров...", reply_markup=srv_kb)
            try:
                await controller._reconcile_orphan_orders()
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
                "� <b>Сервис</b>\n\nВыберите действие:",
                reply_markup=_service_kb().as_markup(),
            )
        elif data == "tg:logs":
            log_text = _read_last_log_lines(50)
            blocks = _format_pre_blocks(log_text, max_len=3500)
            await _send_pre_blocks_cb(callback, blocks)
            await _send_text_cb(callback, "� Логи отправлены", reply_markup=_service_kb().as_markup())
        elif data == "tg:pnl":
            await _send_text_cb(callback, _format_pnl_summary(7), reply_markup=_pnl_kb().as_markup())
        elif data.startswith("tg:pnl:"):
            period = data.split(":", 2)[2] if ":" in data else "7"
            if period == "all":
                days = 0
            else:
                try:
                    days = int(period)
                except Exception:
                    days = 7
            await _send_text_cb(callback, _format_pnl_summary(days), reply_markup=_pnl_kb().as_markup())
        elif data == "tg:arbitrage":
            await _send_text_cb(callback, _format_arbitrage_dashboard(), reply_markup=_arbitrage_dashboard_kb().as_markup())
        elif data == "tg:arb_scan":
            uid = callback.from_user.id if callback.from_user else 0
            size = _get_arb_size(uid)
            text, opps = _format_arbitrage_analysis(10, position_size=size)
            await _send_text_cb(callback, text, reply_markup=_arbitrage_analysis_kb(opps, position_size=size).as_markup())
        elif data == "tg:arb_size_menu":
            uid = callback.from_user.id if callback.from_user else 0
            size = _get_arb_size(uid)
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
            size = _get_arb_size(uid)
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
            size = _get_arb_size(uid)
            await _send_text_cb(callback, f"▶️ Открываю арбитраж {html.escape(pair)} на <b>${size:.0f}</b>...", reply_markup=kb)
            try:
                ok, msg = await arbitrage_service.open_arbitrage(pair, size)
                prefix = "✅" if ok else "❌"
                await _send_text_cb(callback, f"{prefix} {html.escape(msg)}", reply_markup=kb)
                if ok:
                    await _send_text_cb(callback, _format_arbitrage_pnl_summary(), reply_markup=kb)
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
                "• Поставит бота на ПАУЗУ\n\n"
                "⚠️ <b>Вы уверены?</b>",
                reply_markup=confirm_kb.as_markup(),
            )
        elif data == "tg:panic_execute":
            if not controller or not hasattr(controller, "panic_sell_all"):
                await _send_text_cb(callback, "❌ Panic sell недоступен", reply_markup=kb)
                return
            await _send_text_cb(callback, "🛑 Выполняю PANIC SELL...", reply_markup=kb)
            try:
                results = await controller.panic_sell_all()
                await _send_text_cb(callback, "\n".join(results), reply_markup=kb)
            except Exception as e:
                await _send_text_cb(callback, f"❌ Ошибка panic sell: {e}", reply_markup=kb)
        elif data == "tg:menu":
            await _send_text_cb(callback, "📋 Главное меню", reply_markup=kb)

    return router
