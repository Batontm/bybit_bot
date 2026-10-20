"""
Точность цен и количества для спотовых пар Bybit.

Раньше точность была захардкожена таблицами на ~10 пар, а для остальных
использовалось «2 знака по умолчанию». Для дешёвых монет из сканера
(цена < $1) это округляло стоп и тейк до неверных уровней. Теперь значения
берутся из get_instruments_info (tickSize / basePrecision) и кэшируются.
"""
from __future__ import annotations

import math
import threading
import time
from decimal import Decimal
from typing import Dict, Optional, Tuple

from config.settings import logger


# Запасные значения, если API недоступно
_FALLBACK_QTY = {
    'BTCUSDT': 6, 'ETHUSDT': 4, 'SOLUSDT': 2, 'XRPUSDT': 0, 'DOGEUSDT': 0,
    'ADAUSDT': 0, 'AVAXUSDT': 2, 'LINKUSDT': 2, 'DOTUSDT': 2, 'MATICUSDT': 0,
}
_FALLBACK_PRICE = {
    'BTCUSDT': 2, 'ETHUSDT': 2, 'SOLUSDT': 2, 'XRPUSDT': 4, 'DOGEUSDT': 5,
    'ADAUSDT': 4, 'AVAXUSDT': 2, 'LINKUSDT': 2, 'DOTUSDT': 2, 'MATICUSDT': 4,
}


def step_decimals(step: str) -> int:
    """'0.0001' -> 4, '1' -> 0, '0.50' -> 1."""
    try:
        d = Decimal(str(step)).normalize()
        exp = d.as_tuple().exponent
        return max(0, -exp) if isinstance(exp, int) else 0
    except Exception:
        return 2


def floor_to_decimals(value: float, decimals: int) -> float:
    factor = 10 ** decimals
    return math.floor(value * factor + 1e-9) / factor


def fmt(value: float, decimals: int) -> str:
    if decimals <= 0:
        return str(int(value))
    s = f"{value:.{decimals}f}".rstrip('0').rstrip('.')
    return s or "0"


class InstrumentInfo:
    """Кэш параметров инструментов (тик цены, шаг количества)."""

    TTL_SECONDS = 6 * 3600

    def __init__(self, client=None) -> None:
        self._client = client
        self._cache: Dict[str, Tuple[float, int, int]] = {}  # pair -> (ts, qty_dec, price_dec)
        self._lock = threading.Lock()

    def _get_client(self):
        if self._client is None:
            from pybit.unified_trading import HTTP
            from config.api_config import get_pybit_kwargs
            self._client = HTTP(**get_pybit_kwargs())
        return self._client

    def _load(self, pair: str) -> Optional[Tuple[int, int]]:
        try:
            resp = self._get_client().get_instruments_info(category="spot", symbol=pair)
            if resp.get('retCode') != 0 or not resp['result']['list']:
                return None
            info = resp['result']['list'][0]
            qty_dec = step_decimals(info.get('lotSizeFilter', {}).get('basePrecision', '0.01'))
            price_dec = step_decimals(info.get('priceFilter', {}).get('tickSize', '0.01'))
            return qty_dec, price_dec
        except Exception as e:
            logger.debug(f"instrument_info {pair}: {e}")
            return None

    def decimals(self, pair: str) -> Tuple[int, int]:
        """(qty_decimals, price_decimals)"""
        now = time.time()
        with self._lock:
            cached = self._cache.get(pair)
            if cached and now - cached[0] < self.TTL_SECONDS:
                return cached[1], cached[2]
        loaded = self._load(pair)
        if loaded is None:
            return _FALLBACK_QTY.get(pair, 2), _FALLBACK_PRICE.get(pair, 4)
        with self._lock:
            self._cache[pair] = (now, loaded[0], loaded[1])
        return loaded

    def qty_decimals(self, pair: str) -> int:
        return self.decimals(pair)[0]

    def price_decimals(self, pair: str) -> int:
        return self.decimals(pair)[1]

    def format_qty(self, pair: str, quantity: float) -> str:
        """Количество, округлённое ВНИЗ (чтобы не получить Insufficient balance)."""
        dec = self.qty_decimals(pair)
        return fmt(floor_to_decimals(quantity, dec), dec)

    def format_price(self, pair: str, price: float) -> str:
        dec = self.price_decimals(pair)
        return fmt(round(price, dec), dec)


instrument_info = InstrumentInfo()
