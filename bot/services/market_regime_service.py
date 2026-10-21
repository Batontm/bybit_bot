from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from pybit.unified_trading import HTTP

from config.api_config import BYBIT_TESTNET
from config.settings import logger, TIMEZONE
from .indicators_service import TechnicalIndicators, closed_candles


def regime_metrics(candles, ema_period: int = 200, rsi_period: int = 14, atr_windows: int = 30):
    """
    Чистый расчёт показателей светофора по ЗАКРЫТЫМ 4h свечам BTC.
    Returns: (price, ema200, rsi14, atr_ratio) — любое может быть None при нехватке данных.
    Используется живым ботом и бэктестером.
    """
    closes = [c['close'] for c in candles]
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    price = closes[-1] if closes else None
    ema = TechnicalIndicators.calculate_ema(closes, ema_period)
    rsi = TechnicalIndicators.calculate_rsi(closes, rsi_period)
    atr_ratio = None
    atrs = TechnicalIndicators.atr_series(highs, lows, closes, 14)
    if len(atrs) > atr_windows:
        prev = atrs[-(atr_windows + 1):-1]
        avg = sum(prev) / len(prev)
        if avg > 0:
            atr_ratio = atrs[-1] / avg
    return price, ema, rsi, atr_ratio


def regime_allowed(price, ema, rsi, atr_ratio, rsi_min: float = 40, atr_spike: float = 2.0) -> bool:
    if price is None or ema is None or rsi is None:
        return False
    atr_blocked = atr_ratio is not None and atr_ratio > atr_spike
    return price > ema and rsi >= rsi_min and not atr_blocked


@dataclass
class RegimeStatus:
    allowed: bool
    label: str
    reason: str
    checked_at: datetime
    price: Optional[float] = None
    ema200_4h: Optional[float] = None
    rsi14_4h: Optional[float] = None
    atr_ratio: Optional[float] = None  # текущий ATR / средний ATR


class MarketRegimeService:
    """4H market regime filter (“Светофор”).

    Variant B: if regime is bad -> block ALL new trades.

    Heuristics (4H, BTCUSDT, только закрытые свечи):
    - allowed if close > EMA200 and RSI(14) >= RSI_MIN and ATR не в панике
    - ATR-паника: текущий ATR14 > ATR_SPIKE_MULTIPLIER × среднего ATR14 за ATR_AVG_WINDOWS окон
    """

    REGIME_PAIR = "BTCUSDT"
    INTERVAL = "240"  # 4H
    EMA_PERIOD = 200
    RSI_PERIOD = 14
    RSI_MIN = 40
    ATR_SPIKE_MULTIPLIER = 2.0  # Блокировка если ATR > 2x от среднего (паника)
    ATR_AVG_WINDOWS = 30        # Сколько предыдущих значений ATR усреднять

    def __init__(self) -> None:
        # Read-only market data. Use mainnet for stable candles even when trading on testnet.
        self._client = HTTP(testnet=False)
        self._cache: Optional[RegimeStatus] = None
        self._cache_ttl = timedelta(minutes=5)

    def get_status(self, *, force: bool = False) -> RegimeStatus:
        now = datetime.now(TIMEZONE)

        if not force and self._cache and (now - self._cache.checked_at) <= self._cache_ttl:
            return self._cache

        status = self._compute_status(now)
        self._cache = status
        return status

    def is_trading_allowed(self) -> Tuple[bool, str]:
        st = self.get_status()
        return st.allowed, st.reason

    def _compute_status(self, now: datetime) -> RegimeStatus:
        try:
            limit = self.EMA_PERIOD + 50
            resp = self._client.get_kline(
                category="spot",
                symbol=self.REGIME_PAIR,
                interval=self.INTERVAL,
                limit=limit,
            )

            if resp.get("retCode") != 0 or not (resp.get("result") or {}).get("list"):
                reason = f"Режим: нет данных свечей {self.REGIME_PAIR} 4H (retCode={resp.get('retCode')}, msg={resp.get('retMsg')})"
                return RegimeStatus(
                    allowed=False,
                    label="КРАСНЫЙ",
                    reason=reason,
                    checked_at=now,
                )

            candles = closed_candles(resp["result"]["list"])
            # ATR-паника: текущий ATR14 против среднего ATR14 за ATR_AVG_WINDOWS окон
            price, ema200, rsi14, atr_ratio = regime_metrics(
                candles, self.EMA_PERIOD, self.RSI_PERIOD, self.ATR_AVG_WINDOWS)

            if price is None or ema200 is None or rsi14 is None:
                reason = (
                    f"Режим: недостаточно данных для расчёта (price={price}, EMA200={ema200}, RSI14={rsi14})"
                )
                return RegimeStatus(
                    allowed=False,
                    label="КРАСНЫЙ",
                    reason=reason,
                    checked_at=now,
                    price=price,
                    ema200_4h=ema200,
                    rsi14_4h=rsi14,
                    atr_ratio=atr_ratio,
                )

            # ATR spike check — блокировка при панике на рынке
            atr_blocked = atr_ratio is not None and atr_ratio > self.ATR_SPIKE_MULTIPLIER

            ok = regime_allowed(price, ema200, rsi14, atr_ratio, self.RSI_MIN, self.ATR_SPIKE_MULTIPLIER)
            if ok:
                label = "ЗЕЛЕНЫЙ"
                atr_info = f", ATR_ratio={atr_ratio:.2f}" if atr_ratio else ""
                reason = (
                    f"Режим {label}: BTC 4H close={price:.2f} > EMA200={ema200:.2f} и RSI14={rsi14:.2f} >= {self.RSI_MIN}{atr_info}"
                )
            else:
                label = "КРАСНЫЙ"
                parts = []
                if not (price > ema200):
                    parts.append(f"close={price:.2f} <= EMA200={ema200:.2f}")
                if not (rsi14 >= self.RSI_MIN):
                    parts.append(f"RSI14={rsi14:.2f} < {self.RSI_MIN}")
                if atr_blocked:
                    parts.append(f"ATR spike={atr_ratio:.2f}x > {self.ATR_SPIKE_MULTIPLIER}x")
                reason = f"Режим {label}: запрет новых сделок ({', '.join(parts)})"

            return RegimeStatus(
                allowed=ok,
                label=label,
                reason=reason,
                checked_at=now,
                price=price,
                ema200_4h=ema200,
                rsi14_4h=rsi14,
                atr_ratio=atr_ratio,
            )

        except Exception as e:
            logger.warning(f"⚠️ Ошибка market regime filter: {e}")
            return RegimeStatus(
                allowed=False,
                label="КРАСНЫЙ",
                reason=f"Режим КРАСНЫЙ: ошибка расчёта ({e})",
                checked_at=now,
            )


market_regime_service = MarketRegimeService()
