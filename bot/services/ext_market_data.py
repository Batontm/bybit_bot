"""
Открытые рыночные данные других бирж (без API-ключей) — фильтры для трендовой стратегии.

Источники:
  • Binance spot     — дневные свечи (объём): /api/v3/klines
  • Binance futures  — ставки финансирования: /fapi/v1/fundingRate
  • Bybit linear     — ставки финансирования: get_funding_rate_history
  • Coinbase         — дневные свечи BTC-USD / ETH-USD / SOL-USD (премия США)

Из них считаются дневные признаки (по UTC-дню закрытия свечи):
  funding_day  — средняя по биржам сумма ставок финансирования за сутки
                 (сколько платят лонги в день; высокая = «перегретые» лонги);
  volume_ratio — объём дня (Bybit + Binance) / средний объём 20 предыдущих дней
                 (пробой на большом объёме надёжнее);
  cb_premium   — средняя за 3 дня премия Coinbase к Bybit (спрос из США).

Фильтры включаются в config (TREND_EXT_*). По умолчанию выключены: включать только
если бэктест (`python -m bot.backtest --strategy trend --ext`) показал улучшение.
Любой недоступный источник просто пропускается — фильтр по нему не применяется.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Callable, Dict, List, Optional, Tuple

from config import trading_config as cfg

D1 = 86_400_000
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "ext"
UA = {"User-Agent": "bybit-bot/2.7 (+market-data)"}

COINBASE_PRODUCTS = {"BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD", "SOLUSDT": "SOL-USD",
                     "XRPUSDT": "XRP-USD", "DOGEUSDT": "DOGE-USD", "LINKUSDT": "LINK-USD",
                     "AVAXUSDT": "AVAX-USD", "LTCUSDT": "LTC-USD", "ADAUSDT": "ADA-USD"}


def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def http_json(url: str, params: Optional[dict] = None, timeout: float = 15.0, attempts: int = 3):
    """GET → JSON с повторами. Бросает исключение, если источник недоступен."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {last}")


def day_of(ts_ms: int) -> int:
    return ts_ms - ts_ms % D1


# ----------------------------------------------------------------- загрузчики

def binance_daily(symbol: str, start_ms: int, end_ms: int, get=http_json) -> Dict[int, dict]:
    """{начало_дня_ms: {'close','volume'}} — только закрытые дни."""
    out: Dict[int, dict] = {}
    cursor = start_ms
    while cursor < end_ms:
        rows = get("https://api.binance.com/api/v3/klines",
                   {"symbol": symbol, "interval": "1d", "startTime": cursor, "endTime": end_ms, "limit": 1000})
        if not rows:
            break
        for k in rows:
            s = int(k[0])
            if s + D1 <= end_ms:
                out[s] = {'close': float(k[4]), 'volume': float(k[5])}
        nxt = int(rows[-1][0]) + D1
        if nxt <= cursor or len(rows) < 1000:
            break
        cursor = nxt
    return out


def binance_funding(symbol: str, start_ms: int, end_ms: int, get=http_json) -> List[Tuple[int, float]]:
    out: List[Tuple[int, float]] = []
    cursor = start_ms
    while cursor < end_ms:
        rows = get("https://fapi.binance.com/fapi/v1/fundingRate",
                   {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000})
        if not rows:
            break
        out += [(int(r['fundingTime']), float(r['fundingRate'])) for r in rows]
        nxt = int(rows[-1]['fundingTime']) + 1
        if nxt <= cursor or len(rows) < 1000:
            break
        cursor = nxt
    return out


def bybit_funding(symbol: str, start_ms: int, end_ms: int, client=None) -> List[Tuple[int, float]]:
    if client is None:
        from pybit.unified_trading import HTTP
        client = HTTP(testnet=False)
    out: List[Tuple[int, float]] = []
    cursor_end = end_ms
    while cursor_end > start_ms:
        resp = client.get_funding_rate_history(category="linear", symbol=symbol,
                                               startTime=start_ms, endTime=cursor_end, limit=200)
        rows = (resp.get('result') or {}).get('list') or [] if resp.get('retCode') == 0 else []
        if not rows:
            break
        pts = [(int(r['fundingRateTimestamp']), float(r['fundingRate'])) for r in rows]
        out += pts
        oldest = min(t for t, _ in pts)
        if oldest <= start_ms or len(rows) < 200:
            break
        cursor_end = oldest - 1
        time.sleep(0.05)
    return sorted(set(out))


def coinbase_daily(product: str, start_ms: int, end_ms: int, get=http_json) -> Dict[int, dict]:
    """Coinbase отдаёт максимум 300 свечей за запрос: [time_s, low, high, open, close, volume]."""
    out: Dict[int, dict] = {}
    span = 300 * D1
    lo = day_of(start_ms)
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    while lo < end_ms:
        hi = min(lo + span, end_ms)
        rows = get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                   {"granularity": 86400, "start": iso(lo), "end": iso(hi)})
        for k in rows or []:
            s = int(k[0]) * 1000
            if s + D1 <= end_ms:
                out[s] = {'close': float(k[4]), 'volume': float(k[5])}
        lo = hi
        time.sleep(0.15)
    return out


# ----------------------------------------------------------------- признаки (чистые функции)

def funding_by_day(points: List[Tuple[int, float]]) -> Dict[int, float]:
    """Сумма ставок за каждые сутки UTC (= стоимость лонга за день)."""
    out: Dict[int, float] = {}
    for t, r in points:
        d = day_of(t)
        out[d] = out.get(d, 0.0) + r
    return out


def build_features(bybit_days: List[dict], *, binance_days: Optional[Dict[int, dict]] = None,
                   funding_sources: Optional[List[Dict[int, float]]] = None,
                   coinbase_days: Optional[Dict[int, dict]] = None,
                   vol_window: int = 20, premium_days: int = 3, funding_days: int = 3) -> Dict[int, dict]:
    """
    {начало_дня_ms: {'funding_day', 'volume_ratio', 'cb_premium'}} — значения None,
    если источника нет. Признак дня использует только данные этого дня и прошлых.
    """
    binance_days = binance_days or {}
    funding_sources = [f for f in (funding_sources or []) if f]
    coinbase_days = coinbase_days or {}
    feats: Dict[int, dict] = {}
    vols: List[float] = []
    prem_hist: List[float] = []
    for d in sorted(bybit_days, key=lambda x: x['start']):
        s = d['start']
        f: Dict[str, Optional[float]] = {'funding_day': None, 'volume_ratio': None, 'cb_premium': None}

        # объём: Bybit + Binance (если есть за этот день)
        v = d['volume'] + (binance_days[s]['volume'] if s in binance_days else 0.0)
        if binance_days and len(vols) >= vol_window:
            base = mean(vols[-vol_window:])
            f['volume_ratio'] = v / base if base > 0 else None
        if binance_days:
            vols.append(v)

        # funding: среднее по биржам, среднее за последние funding_days суток
        if funding_sources:
            per_day = []
            for k in range(funding_days):
                day = s - k * D1
                vals = [src[day] for src in funding_sources if day in src]
                if vals:
                    per_day.append(mean(vals))
            f['funding_day'] = mean(per_day) if per_day else None

        # премия Coinbase к Bybit
        if s in coinbase_days and d['close'] > 0:
            prem_hist.append(coinbase_days[s]['close'] / d['close'] - 1)
            f['cb_premium'] = mean(prem_hist[-premium_days:])
        feats[s] = f
    return feats


def ext_filter(feat: Optional[dict]) -> Tuple[bool, str]:
    """Проверка включённых фильтров для дня пробоя. Нет данных — фильтр не мешает."""
    if not feat:
        return True, "нет данных других бирж"
    if cfg.TREND_EXT_FUNDING and feat.get('funding_day') is not None \
            and feat['funding_day'] > cfg.TREND_EXT_FUNDING_MAX_DAY:
        return False, (f"funding {feat['funding_day'] * 100:.3f}%/сут > "
                       f"{cfg.TREND_EXT_FUNDING_MAX_DAY * 100:.3f}% — лонги перегреты")
    if cfg.TREND_EXT_VOLUME and feat.get('volume_ratio') is not None \
            and feat['volume_ratio'] < cfg.TREND_EXT_VOLUME_MIN:
        return False, f"объём пробоя {feat['volume_ratio']:.2f}× < {cfg.TREND_EXT_VOLUME_MIN}× среднего (Bybit+Binance)"
    if cfg.TREND_EXT_PREMIUM and feat.get('cb_premium') is not None \
            and feat['cb_premium'] < cfg.TREND_EXT_PREMIUM_MIN:
        return False, f"премия Coinbase {feat['cb_premium'] * 100:+.3f}% — спроса из США нет"
    return True, "ок"


def any_filter_enabled() -> bool:
    return bool(cfg.TREND_EXT_FUNDING or cfg.TREND_EXT_VOLUME or cfg.TREND_EXT_PREMIUM)


# ----------------------------------------------------------------- загрузка с кэшем (бэктест)

def _cache_get(name: str, loader: Callable[[], object], max_age_h: float = 12.0, cache_dir: Optional[Path] = None):
    path = (cache_dir or CACHE_DIR) / f"{name}.json"
    if path.exists() and time.time() - path.stat().st_mtime < max_age_h * 3600:
        return json.loads(path.read_text())
    data = loader()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def load_ext(pair: str, start_ms: int, end_ms: int, *, bybit_client=None, get=http_json,
             cache_dir: Optional[Path] = None, use_cache: bool = True, log=print) -> Dict[str, object]:
    """Скачать все источники для пары. Возвращает {'binance_days', 'funding_sources', 'coinbase_days', 'status'}."""
    status: Dict[str, str] = {}
    tag = f"{pair}_{day_of(start_ms)}"

    def fetch(name, fn):
        try:
            if use_cache:
                v = _cache_get(f"{name}_{tag}", fn, cache_dir=cache_dir)
            else:
                v = fn()
            status[name] = f"ок ({len(v)})"
            return v
        except Exception as e:
            status[name] = f"недоступно: {str(e)[:80]}"
            log(f"  ⚠️ {pair} {name}: {status[name]}")
            return None

    bn_days = fetch("binance_spot", lambda: {str(k): v for k, v in binance_daily(pair, start_ms, end_ms, get).items()})
    bn_fund = fetch("binance_funding", lambda: binance_funding(pair, start_ms, end_ms, get))
    by_fund = fetch("bybit_funding", lambda: bybit_funding(pair, start_ms, end_ms, bybit_client))
    product = COINBASE_PRODUCTS.get(pair)
    cb_days = fetch("coinbase", lambda: {str(k): v for k, v in coinbase_daily(product, start_ms, end_ms, get).items()}) \
        if product else None
    return {
        'binance_days': {int(k): v for k, v in (bn_days or {}).items()},
        'funding_sources': [funding_by_day([tuple(x) for x in src]) for src in (bn_fund, by_fund) if src],
        'coinbase_days': {int(k): v for k, v in (cb_days or {}).items()},
        'status': status,
    }


def features_for(pair: str, bybit_days: List[dict], start_ms: int, end_ms: int, **kw) -> Tuple[Dict[int, dict], Dict[str, str]]:
    src = load_ext(pair, start_ms, end_ms, **kw)
    feats = build_features(bybit_days, binance_days=src['binance_days'],
                           funding_sources=src['funding_sources'], coinbase_days=src['coinbase_days'])
    return feats, src['status']
