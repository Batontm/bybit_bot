"""
Загрузка истории свечей Bybit (spot, mainnet, публичный API) с кэшем на диске.

Кэш: data/klines/<SYMBOL>_<interval>.csv — повторный запуск докачивает только новое.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Dict, List, Optional

INTERVAL_MS = {"15": 15 * 60_000, "60": 60 * 60_000, "240": 240 * 60_000, "D": 1440 * 60_000}
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "klines"


def _client():
    from pybit.unified_trading import HTTP
    return HTTP(testnet=False)


def _read_cache(path: Path) -> Dict[int, dict]:
    rows: Dict[int, dict] = {}
    if not path.exists():
        return rows
    with path.open() as f:
        for r in csv.DictReader(f):
            start = int(r['start'])
            rows[start] = {'start': start, 'open': float(r['open']), 'high': float(r['high']),
                           'low': float(r['low']), 'close': float(r['close']),
                           'volume': float(r['volume']), 'turnover': float(r.get('turnover') or 0)}
    return rows


def _write_cache(path: Path, rows: Dict[int, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['start', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])
    tmp.replace(path)


def _get_kline_retry(client, symbol: str, interval: str, lo: int, cursor_end: int, log,
                     attempts: int = 5) -> dict:
    """
    Запрос страницы свечей с повторами. Bybit иногда отвечает «internal error»
    (10016/10006/10002) — это временный сбой, повторяем с паузой; с 3-й попытки
    запрашиваем без start (только end+limit): он короче и реже падает.
    """
    last = None
    for i in range(attempts):
        params = dict(category="spot", symbol=symbol, interval=interval, end=cursor_end, limit=1000)
        if i < 2:
            params['start'] = lo
        try:
            resp = client.get_kline(**params)
            if resp.get('retCode') == 0:
                return resp
            last = RuntimeError(f"{symbol} {interval}: {resp.get('retMsg')} ({resp.get('retCode')})")
        except Exception as e:   # pybit бросает InvalidRequestError / сетевые ошибки
            last = e
        wait = 2 * (i + 1)
        log(f"  ⚠️ {symbol} {interval}: ошибка Bybit, повтор через {wait} с ({str(last)[:80]})")
        time.sleep(wait)
    raise RuntimeError(f"{symbol} {interval}: не удалось скачать свечи после {attempts} попыток: {last}")


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int, *, client=None,
                 cache_dir: Optional[Path] = None, offline: bool = False,
                 log=print) -> List[dict]:
    """
    Закрытые свечи [start_ms, end_ms) от старых к новым. Bybit отдаёт максимум 1000
    свечей за запрос, поэтому качаем страницами назад от end_ms.
    """
    step = INTERVAL_MS[interval]
    path = (cache_dir or CACHE_DIR) / f"{symbol}_{interval}.csv"
    rows = _read_cache(path)
    now_ms = int(time.time() * 1000)
    end_ms = min(end_ms, now_ms - (now_ms % step))       # только закрытые свечи

    have = sorted(k for k in rows if start_ms <= k < end_ms)
    need_ranges = []
    if not have:
        need_ranges.append((start_ms, end_ms))
    else:
        if have[0] > start_ms:
            need_ranges.append((start_ms, have[0]))
        if have[-1] + step < end_ms:
            need_ranges.append((have[-1] + step, end_ms))

    if need_ranges and not offline:
        client = client or _client()
        added = 0
        for lo, hi in need_ranges:
            cursor_end = hi - 1
            while cursor_end >= lo:
                try:
                    resp = _get_kline_retry(client, symbol, interval, lo, cursor_end, log)
                except Exception:
                    if added:
                        _write_cache(path, rows)   # не теряем уже скачанное
                    raise
                batch = (resp.get('result') or {}).get('list') or []
                if not batch:
                    break
                oldest = None
                for k in batch:
                    s = int(k[0])
                    if lo <= s < hi and s + step <= now_ms:
                        rows[s] = {'start': s, 'open': float(k[1]), 'high': float(k[2]), 'low': float(k[3]),
                                   'close': float(k[4]), 'volume': float(k[5]),
                                   'turnover': float(k[6]) if len(k) > 6 else 0.0}
                        added += 1
                    oldest = s if oldest is None else min(oldest, s)
                if oldest is None or oldest <= lo:
                    break
                cursor_end = oldest - 1
                time.sleep(0.05)   # бережём лимиты API
        if added:
            _write_cache(path, rows)
            log(f"  {symbol} {interval}: +{added} свечей (в кэше {len(rows)})")

    return [rows[k] for k in sorted(rows) if start_ms <= k < end_ms]


def load_dataset(pairs: List[str], days: int, *, warmup_days: int = 60, client=None,
                 cache_dir: Optional[Path] = None, offline: bool = False, log=print) -> Dict[str, Dict[str, List[dict]]]:
    """{pair: {'15': [...], '60': [...], '240': [...]}} + BTCUSDT всегда (для светофора и силы к BTC)."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days + warmup_days) * 86_400_000
    data: Dict[str, Dict[str, List[dict]]] = {}
    for pair in dict.fromkeys(['BTCUSDT'] + list(pairs)):
        log(f"📥 {pair}...")
        try:
            data[pair] = {tf: fetch_klines(pair, tf, start_ms, end_ms, client=client, cache_dir=cache_dir,
                                           offline=offline, log=log)
                          for tf in ("15", "60", "240")}
        except Exception as e:
            if pair == 'BTCUSDT':
                raise   # без BTC бэктест невозможен
            log(f"  ⚠️ {pair} пропущена: {e}")
    return data
