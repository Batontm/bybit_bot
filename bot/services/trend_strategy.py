"""
Трендовая стратегия на дневных свечах (BTC/ETH/SOL).

Правила (классика следования за трендом, мало параметров):
  вход   — дневное закрытие выше максимума предыдущих TREND_BREAKOUT_DAYS дней
           и выше EMA(TREND_FILTER_EMA);
  стоп   — вход − TREND_STOP_ATR × ATR(14) дневной;
  выход  — «люстра»: максимум закрытий с момента входа − TREND_TRAIL_ATR × ATR,
           стоп только поднимается; тейка нет.
Размер позиции — от стопа с учётом комиссий (exit_rules.position_size),
поэтому волатильные монеты автоматически берутся меньшим количеством.

Чистые функции ниже используются и живым ботом, и бэктестером.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from config import trading_config as cfg
from .indicators_service import TechnicalIndicators as TI

D1 = 86_400_000


def daily_from_4h(candles_4h: List[dict]) -> List[dict]:
    """Склеить 4h свечи в дневные (UTC, по 6 штук), только полные дни."""
    days: Dict[int, List[dict]] = {}
    for c in candles_4h:
        days.setdefault(c['start'] // D1, []).append(c)
    out = []
    for d in sorted(days):
        ch = sorted(days[d], key=lambda x: x['start'])
        if len(ch) < 6:
            continue
        out.append({'start': d * D1, 'open': ch[0]['open'], 'high': max(x['high'] for x in ch),
                    'low': min(x['low'] for x in ch), 'close': ch[-1]['close'],
                    'volume': sum(x['volume'] for x in ch)})
    return out


def daily_atr(days: List[dict]) -> Optional[float]:
    series = TI.atr_series([d['high'] for d in days], [d['low'] for d in days],
                           [d['close'] for d in days], cfg.TREND_ATR_PERIOD)
    return series[-1] if series else None


def entry_signal(days: List[dict]) -> Tuple[bool, Dict]:
    """Сигнал на последней ЗАКРЫТОЙ дневной свече."""
    n = cfg.TREND_BREAKOUT_DAYS
    info: Dict = {}
    if len(days) < max(cfg.TREND_FILTER_EMA, n) + 2:
        info['reason'] = 'мало дневных свечей'
        return False, info
    closes = [d['close'] for d in days]
    ema = TI.ema_series(closes, cfg.TREND_FILTER_EMA)[-1]
    breakout_level = max(d['high'] for d in days[-n - 1:-1])
    atr = daily_atr(days)
    close = closes[-1]
    info.update({'close': close, 'ema': ema, 'breakout_level': breakout_level, 'atr': atr})
    if not atr:
        info['reason'] = 'нет ATR'
        return False, info
    if close <= ema:
        info['reason'] = f'цена ниже EMA{cfg.TREND_FILTER_EMA}'
        return False, info
    if close <= breakout_level:
        info['reason'] = f'нет пробоя {n}-дневного максимума'
        return False, info
    info['stop'] = close - cfg.TREND_STOP_ATR * atr
    return True, info


def chandelier_stop(current_stop: float, highest_close: float, atr: float) -> float:
    """Трейлинг «люстра»: стоп = максимум закрытий − k×ATR, только вверх."""
    if not atr or atr <= 0:
        return current_stop
    return max(current_stop, highest_close - cfg.TREND_TRAIL_ATR * atr)


# ----------------------------------------------------------------- живой бот

class TrendService:
    """Раз в сутки (после закрытия дневной свечи UTC) — входы и подтяжка стопов."""

    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from pybit.unified_trading import HTTP
            self._client = HTTP(testnet=False)   # рыночные данные — mainnet
        return self._client

    def _daily(self, pair: str, limit: int = 200) -> List[dict]:
        from .indicators_service import closed_candles
        resp = self.client.get_kline(category="spot", symbol=pair, interval="D", limit=limit)
        if resp.get('retCode') != 0:
            return []
        return closed_candles(resp['result']['list'])

    async def run(self, trading_service, position_service, allow_entries: bool = True) -> List[str]:
        if not cfg.TREND_ENABLED:
            return []
        messages: List[str] = []
        open_positions = trading_service.trades_repo.get_open_positions()
        by_pair = {p['pair']: p for p in open_positions}
        try:
            from .arbitrage_service import arbitrage_service
            arb_pairs = {p['pair'] for p in arbitrage_service.repo.get_open_positions()}
        except Exception:
            arb_pairs = set()

        for pair in cfg.TREND_PAIRS:
            try:
                days = self._daily(pair)
            except Exception as e:
                messages.append(f"⚠️ Тренд {pair}: нет дневных свечей ({e})")
                continue
            if not days:
                continue
            pos = by_pair.get(pair)

            # --- сопровождение открытой трендовой позиции
            if pos and pos.get('strategy') == 'TREND':
                from datetime import datetime
                try:
                    opened_ms = int(datetime.fromisoformat(pos['opened_at']).timestamp() * 1000)
                except Exception:
                    opened_ms = days[-1]['start']
                since = [d['close'] for d in days if d['start'] + D1 > opened_ms] or [days[-1]['close']]
                atr = daily_atr(days) or float(pos.get('entry_atr') or 0)
                cur = float(pos.get('sl_price') or 0)
                new_sl = chandelier_stop(cur, max(since), atr)
                if new_sl > cur * 1.002 and new_sl < days[-1]['close']:
                    if await position_service._amend_sl(pos['id'], pair, new_sl):
                        messages.append(f"📈 Тренд {pair}: стоп подтянут {cur:.6g} → {new_sl:.6g}")
                continue
            if pos or pair in arb_pairs or not allow_entries:
                continue   # по паре уже есть позиция другой стратегии / входы запрещены

            # --- новый вход
            ok, info = entry_signal(days)
            if not ok:
                continue
            res, err = await trading_service.open_trend_position(pair, info['stop'], info['atr'])
            if res:
                risk_pct = (res['entry_price'] - res['sl_price']) / res['entry_price'] * 100
                messages.append(
                    f"🐢 Тренд: открыта {pair} @ {res['entry_price']:.6g} — пробой {cfg.TREND_BREAKOUT_DAYS}-дн. "
                    f"максимума {info['breakout_level']:.6g}; стоп {res['sl_price']:.6g} (−{risk_pct:.1f}%), "
                    f"дальше трейлинг {cfg.TREND_TRAIL_ATR}×ATR")
            elif err and 'лимит' not in err:
                messages.append(f"⚠️ Тренд {pair}: вход не выполнен — {err}")
        return messages


trend_service = TrendService()
