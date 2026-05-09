from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from pybit.unified_trading import HTTP

from config.api_config import BYBIT_TESTNET
from config.settings import logger, TIMEZONE
from .indicators_service import TechnicalIndicators


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

    Heuristics (4H, BTCUSDT):
    - allowed if close > EMA200 and RSI(14) >= 50
    """

    REGIME_PAIR = "BTCUSDT"
    INTERVAL = "240"  # 4H
    EMA_PERIOD = 200
    RSI_PERIOD = 14
    ATR_SPIKE_MULTIPLIER = 2.0  # Блокировка если ATR > 2x от среднего (паника)

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

            candles = list(reversed(resp["result"]["list"]))
            closes = [float(c[4]) for c in candles]
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]

            price = closes[-1] if closes else None
            ema200 = TechnicalIndicators.calculate_ema(closes, self.EMA_PERIOD)
            rsi14 = TechnicalIndicators.calculate_rsi(closes, self.RSI_PERIOD)

            # ATR volatility spike detection
            atr_ratio = None
            if len(closes) >= 31:
                current_atr = TechnicalIndicators.calculate_atr(highs, lows, closes, 14)
                # Средний ATR за предыдущие 30 свечей (исключая последнюю)
                avg_atr = TechnicalIndicators.calculate_atr(highs[:-1], lows[:-1], closes[:-1], 14)
                if current_atr and avg_atr and avg_atr > 0:
                    atr_ratio = current_atr / avg_atr

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

            ok = (price > ema200) and (rsi14 >= 40) and (not atr_blocked)
            if ok:
                label = "ЗЕЛЕНЫЙ"
                atr_info = f", ATR_ratio={atr_ratio:.2f}" if atr_ratio else ""
                reason = (
                    f"Режим {label}: BTC 4H close={price:.2f} > EMA200={ema200:.2f} и RSI14={rsi14:.2f} >= 50{atr_info}"
                )
            else:
                label = "КРАСНЫЙ"
                parts = []
                if not (price > ema200):
                    parts.append(f"close={price:.2f} <= EMA200={ema200:.2f}")
                if not (rsi14 >= 40):
                    parts.append(f"RSI14={rsi14:.2f} < 50")
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
