"""
Защита счёта: пауза после серии убытков и остановка при просадке от пика.

Сопровождение открытых позиций (стопы, тейки, безубыток) продолжается —
блокируются только НОВЫЕ входы. Состояние хранится в таблице settings,
поэтому переживает перезапуск бота.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional, Tuple

from config import trading_config as cfg
from config.settings import logger, TIMEZONE
from ..db.connection import get_db, get_transaction


def _get(key: str) -> Optional[str]:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row['value'] if row else None
    except Exception:
        return None


def _set(key: str, value) -> None:
    with get_transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


class AccountGuard:

    # ------------------------------------------------------------ серия убытков

    @staticmethod
    def loss_streak() -> Tuple[int, Optional[datetime]]:
        """Сколько последних закрытых сделок подряд убыточны и когда закрыта последняя."""
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT net_pnl, realized_pnl, closed_at FROM positions "
                    "WHERE status = 'CLOSED' AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 50"
                ).fetchall()
        except Exception:
            return 0, None
        streak, last = 0, None
        for r in rows:
            pnl = r['net_pnl'] if r['net_pnl'] not in (None, 0) else r['realized_pnl']
            if pnl is None or float(pnl) >= 0:
                break
            if last is None:
                try:
                    last = datetime.fromisoformat(str(r['closed_at']).replace('Z', '+00:00'))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=TIMEZONE)
                except Exception:
                    last = None
            streak += 1
        return streak, last

    def streak_pause_until(self) -> Optional[datetime]:
        streak, last = self.loss_streak()
        if streak < cfg.MAX_CONSECUTIVE_LOSSES or last is None:
            return None
        ack = _get('loss_streak_ack')      # «возобновить» вручную до конца паузы
        if ack and ack == last.isoformat():
            return None
        from datetime import timedelta
        until = last + timedelta(hours=cfg.LOSS_STREAK_COOLDOWN_HOURS)
        return until if datetime.now(TIMEZONE) < until else None

    # ------------------------------------------------------------ просадка

    def update_equity(self, equity: Optional[float]) -> Optional[str]:
        """Обновить пик капитала; при просадке ≥ MAX_DRAWDOWN_STOP — остановить входы."""
        if not equity or equity <= 0:
            return None
        peak = float(_get('equity_peak') or 0)
        if equity > peak:
            _set('equity_peak', equity)
            return None
        dd = 1 - equity / peak if peak > 0 else 0.0
        if dd >= cfg.MAX_DRAWDOWN_STOP and _get('drawdown_halt') != '1':
            _set('drawdown_halt', '1')
            msg = (f"⛔️ Просадка {dd * 100:.1f}% от пика ${peak:,.0f} (сейчас ${equity:,.0f}). "
                   f"Новые входы остановлены. Открытые позиции сопровождаются. "
                   f"Возобновить — «⚙️ Настройки → Снять стоп по просадке».")
            logger.warning(msg)
            return msg
        return None

    def reset_drawdown(self, equity: Optional[float] = None) -> None:
        """Снять стоп по просадке; пик отсчитывается заново от текущего капитала."""
        _set('drawdown_halt', '0')
        if equity:
            _set('equity_peak', equity)

    def ack_loss_streak(self) -> None:
        _, last = self.loss_streak()
        if last:
            _set('loss_streak_ack', last.isoformat())

    # ------------------------------------------------------------ итог

    def entries_allowed(self) -> Tuple[bool, str]:
        if _get('drawdown_halt') == '1':
            peak = float(_get('equity_peak') or 0)
            return False, f"стоп по просадке ≥ {cfg.MAX_DRAWDOWN_STOP * 100:.0f}% от пика ${peak:,.0f}"
        until = self.streak_pause_until()
        if until:
            streak, _ = self.loss_streak()
            return False, f"{streak} убытков подряд — пауза входов до {until.strftime('%d.%m %H:%M')}"
        return True, ""

    def status(self) -> Dict:
        streak, _ = self.loss_streak()
        peak = float(_get('equity_peak') or 0)
        return {
            'drawdown_halt': _get('drawdown_halt') == '1',
            'equity_peak': peak,
            'loss_streak': streak,
            'streak_pause_until': self.streak_pause_until(),
        }


account_guard = AccountGuard()
