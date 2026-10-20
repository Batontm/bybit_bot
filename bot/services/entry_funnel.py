"""
Воронка входа — чистая логика без сетевых запросов.

Каждая функция принимает уже загруженные данные Bybit (тикеры, свечи, стакан,
сделки, OI) и возвращает решение + числа, которые пишутся в лог/БД. Так всё
можно проверить юнит-тестами и потом сопоставить с результатами сделок.

Шаги:
  1. select_universe      — ликвидность, спред, изменение за 24ч, наличие перпа
  2. check_trend_4h       — цена > EMA50 4h и EMA50 растёт            (обязательно)
     check_pullback_1h    — откат к EMA20/EMA50 1h, RSI в зоне паузы  (баллы)
     relative_strength    — монета сильнее BTC за 24ч и 7 дней        (баллы)
  3. check_trigger_15m    — разворотная свеча / возврат над EMA20 / объём (обязательно)
  4. orderbook_metrics, aggressor_share, oi_signal, funding, long/short    (баллы / вето)
  5. score_candidate      — итоговый балл 0..100 + список вето
     build_trade_plan     — стоп за минимумом отката, тейк в R, проверка ширины стопа
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from config import trading_config as cfg
from .indicators_service import TechnicalIndicators as TI

Candle = Dict[str, float]  # keys: start, open, high, low, close, volume


# Максимальные баллы по компонентам (сумма = 100)
WEIGHTS = {
    'trend_4h': 20,
    'pullback_1h': 15,
    'trigger_15m': 20,
    'relative_strength': 10,
    'orderbook': 8,
    'aggressor': 7,
    'open_interest': 10,
    'funding': 10,
}


# ============================================================ шаг 1: отбор пар

def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def select_universe(spot_tickers: List[Dict], linear_tickers: List[Dict],
                    always_include: Sequence[str] = (),
                    exclude: Sequence[str] = ()) -> List[Dict]:
    """
    Отобрать торгуемые пары из ответа get_tickers.

    Returns: список dict(symbol, turnover, spread, change_24h, funding_rate, has_perp, price),
             отсортированный по обороту (desc), не длиннее FUNNEL_UNIVERSE_SIZE
             (пары из always_include добавляются сверх лимита, если проходят фильтры).
    """
    perps = {t.get('symbol'): t for t in (linear_tickers or [])}
    excluded_bases = set(cfg.FUNNEL_EXCLUDED_BASES)
    exclude = set(exclude)
    lev_suffixes = ('2L', '2S', '3L', '3S', '5L', '5S')

    out = []
    for t in spot_tickers or []:
        symbol = t.get('symbol', '')
        if not symbol.endswith('USDT') or symbol in exclude:
            continue
        base = symbol[:-4]
        if base in excluded_bases or base.endswith(lev_suffixes):
            continue

        price = _f(t.get('lastPrice'))
        bid, ask = _f(t.get('bid1Price')), _f(t.get('ask1Price'))
        turnover = _f(t.get('turnover24h'))
        change = _f(t.get('price24hPcnt')) * 100
        if price <= 0 or bid <= 0 or ask <= 0 or ask < bid:
            continue
        spread = (ask - bid) / ((ask + bid) / 2)

        if turnover < cfg.FUNNEL_MIN_TURNOVER_USDT:
            continue
        if spread > cfg.FUNNEL_MAX_SPREAD:
            continue
        if not (cfg.FUNNEL_CHANGE_24H_MIN <= change <= cfg.FUNNEL_CHANGE_24H_MAX):
            continue
        perp = perps.get(symbol)
        if cfg.FUNNEL_REQUIRE_PERP and perp is None:
            continue

        out.append({
            'symbol': symbol,
            'price': price,
            'turnover': turnover,
            'spread': spread,
            'change_24h': change,
            'has_perp': perp is not None,
            'funding_rate': _f(perp.get('fundingRate')) if perp else None,
        })

    out.sort(key=lambda x: x['turnover'], reverse=True)
    top = out[:cfg.FUNNEL_UNIVERSE_SIZE]
    top_symbols = {x['symbol'] for x in top}
    extra = [x for x in out if x['symbol'] in set(always_include) and x['symbol'] not in top_symbols]
    return top + extra


# ============================================================ шаг 2: сетап

def check_trend_4h(candles: List[Candle]) -> Tuple[bool, Dict]:
    """Цена выше EMA50 (4h) и EMA50 растёт за FUNNEL_TREND_SLOPE_BARS свечей."""
    period = cfg.FUNNEL_TREND_EMA
    closes = [c['close'] for c in candles]
    ema = TI.ema_series(closes, period)
    info: Dict = {}
    if len(ema) <= cfg.FUNNEL_TREND_SLOPE_BARS:
        info['reason'] = 'мало свечей 4h'
        return False, info
    last, past = ema[-1], ema[-1 - cfg.FUNNEL_TREND_SLOPE_BARS]
    info.update({'ema50_4h': last, 'ema50_slope': (last - past) / past if past else 0.0,
                 'close_4h': closes[-1]})
    ok = closes[-1] > last and last > past
    if not ok:
        info['reason'] = 'нет восходящего тренда 4h'
    return ok, info


def check_pullback_1h(candles: List[Candle]) -> Tuple[bool, Dict]:
    """
    Откат на 1h: минимум последних FUNNEL_PULLBACK_BARS свечей подходил к EMA20 или EMA50
    (в пределах FUNNEL_PULLBACK_ZONE_ATR × ATR), RSI(14) в зоне паузы, цена над EMA50.
    Всегда возвращает atr/pullback_low — они нужны для стопа даже без отката.
    """
    closes = [c['close'] for c in candles]
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    info: Dict = {}

    ema20 = TI.ema_series(closes, 20)
    ema50 = TI.ema_series(closes, 50)
    atrs = TI.atr_series(highs, lows, closes, 14)
    rsi = TI.calculate_rsi(closes, 14)
    if not ema20 or not ema50 or not atrs or rsi is None:
        info['reason'] = 'мало свечей 1h'
        return False, info

    atr = atrs[-1]
    n = cfg.FUNNEL_PULLBACK_BARS
    pullback_low = min(lows[-n:])
    zone = cfg.FUNNEL_PULLBACK_ZONE_ATR * atr
    touched_ema20 = pullback_low <= ema20[-1] + zone
    touched_ema50 = pullback_low <= ema50[-1] + zone
    info.update({
        'atr_1h': atr, 'ema20_1h': ema20[-1], 'ema50_1h': ema50[-1], 'rsi_1h': rsi,
        'pullback_low': pullback_low, 'close_1h': closes[-1],
    })

    rsi_ok = cfg.FUNNEL_PULLBACK_RSI_MIN <= rsi <= cfg.FUNNEL_PULLBACK_RSI_MAX
    above_ema50 = closes[-1] > ema50[-1]
    ok = (touched_ema20 or touched_ema50) and rsi_ok and above_ema50
    if not ok:
        parts = []
        if not (touched_ema20 or touched_ema50):
            parts.append('нет отката к EMA20/50')
        if not rsi_ok:
            parts.append(f'RSI1h={rsi:.0f} вне {cfg.FUNNEL_PULLBACK_RSI_MIN}-{cfg.FUNNEL_PULLBACK_RSI_MAX}')
        if not above_ema50:
            parts.append('close ниже EMA50 1h')
        info['reason'] = ', '.join(parts)
    return ok, info


def relative_strength(coin_4h: List[Candle], btc_4h: List[Candle],
                      coin_change_24h: float, btc_change_24h: float) -> Tuple[bool, Dict]:
    """Монета сильнее BTC и за 24ч, и за FUNNEL_RS_DAYS дней."""
    bars = cfg.FUNNEL_RS_DAYS * 6
    info: Dict = {'rs_24h': coin_change_24h - btc_change_24h}
    if len(coin_4h) <= bars or len(btc_4h) <= bars:
        info['reason'] = 'мало свечей для RS'
        return False, info
    coin_7d = coin_4h[-1]['close'] / coin_4h[-1 - bars]['close'] - 1
    btc_7d = btc_4h[-1]['close'] / btc_4h[-1 - bars]['close'] - 1
    info['rs_7d'] = (coin_7d - btc_7d) * 100
    ok = info['rs_24h'] > 0 and info['rs_7d'] > 0
    return ok, info


# ============================================================ шаг 3: триггер

def check_trigger_15m(candles: List[Candle], current_price: float) -> Tuple[bool, Dict]:
    """
    Триггер на младшем ТФ — одно из, в последних FUNNEL_TRIGGER_LOOKBACK закрытых свечах:
      * break_high  — зелёная свеча закрылась выше максимума предыдущей
      * reclaim     — закрытие выше EMA20(15m) после закрытия ниже неё
      * volume      — зелёная свеча с объёмом > FUNNEL_TRIGGER_VOLUME_MULT × среднего(20)
    Плюс защита от погони: цена не ушла выше закрытия свечи-триггера больше FUNNEL_MAX_CHASE.
    """
    info: Dict = {}
    if len(candles) < 25:
        info['reason'] = 'мало свечей 15m'
        return False, info

    closes = [c['close'] for c in candles]
    ema20 = TI.ema_series(closes, 20)
    # выравниваем EMA с индексами свечей
    ema_by_idx = {len(closes) - len(ema20) + i: v for i, v in enumerate(ema20)}

    lookback = cfg.FUNNEL_TRIGGER_LOOKBACK
    found = None
    for idx in range(len(candles) - 1, len(candles) - 1 - lookback, -1):
        c, prev = candles[idx], candles[idx - 1]
        green = c['close'] > c['open']
        kinds = []
        if green and c['close'] > prev['high']:
            kinds.append('break_high')
        e_now, e_prev = ema_by_idx.get(idx), ema_by_idx.get(idx - 1)
        if e_now is not None and e_prev is not None and prev['close'] < e_prev and c['close'] > e_now:
            kinds.append('reclaim')
        vols = [x['volume'] for x in candles[max(0, idx - 20):idx]]
        avg_vol = sum(vols) / len(vols) if vols else 0
        if green and avg_vol > 0 and c['volume'] > cfg.FUNNEL_TRIGGER_VOLUME_MULT * avg_vol:
            kinds.append('volume')
        if kinds:
            found = (idx, kinds, c)
            break

    if not found:
        info['reason'] = 'нет триггера на 15m'
        return False, info

    idx, kinds, c = found
    chase = (current_price - c['close']) / c['close'] if c['close'] else 0.0
    info.update({'trigger': '+'.join(kinds), 'trigger_close': c['close'],
                 'trigger_age_bars': len(candles) - 1 - idx, 'chase': chase})
    if chase > cfg.FUNNEL_MAX_CHASE:
        info['reason'] = f'цена ушла на {chase*100:.2f}% от триггера'
        return False, info
    return True, info


# ============================================================ шаг 4: поток ордеров

def orderbook_metrics(bids: List[List], asks: List[List], price: float) -> Dict:
    """
    Дисбаланс стакана в USDT в пределах ±FUNNEL_BOOK_DEPTH_PCT и поиск «стены» асков
    в пределах 0.5% над ценой.
    """
    depth = cfg.FUNNEL_BOOK_DEPTH_PCT
    b = [(float(p), float(q)) for p, q, *_ in bids]
    a = [(float(p), float(q)) for p, q, *_ in asks]
    bid_usd = sum(p * q for p, q in b if p >= price * (1 - depth))
    ask_levels = [(p, q) for p, q in a if p <= price * (1 + depth)]
    ask_usd = sum(p * q for p, q in ask_levels)
    imbalance = bid_usd / ask_usd if ask_usd > 0 else None

    ask_wall = False
    near = [p * q for p, q in ask_levels if p <= price * 1.005]
    all_sizes = [p * q for p, q in ask_levels]
    if near and len(all_sizes) >= 5:
        med = statistics.median(all_sizes)
        if med > 0 and max(near) > cfg.FUNNEL_ASK_WALL_MULT * med and max(near) > 0.2 * ask_usd:
            ask_wall = True
    return {'book_imbalance': imbalance, 'bid_usd_1pct': bid_usd,
            'ask_usd_1pct': ask_usd, 'ask_wall': ask_wall}


def aggressor_share(trades: List[Dict], now_ms: int) -> Optional[float]:
    """Доля объёма рыночных покупок (taker side = Buy) за последние N минут."""
    window = cfg.FUNNEL_AGGRESSOR_WINDOW_MIN * 60_000
    buy = sell = 0.0
    for t in trades or []:
        ts = int(_f(t.get('time')))
        if now_ms - ts > window:
            continue
        size = _f(t.get('size')) * _f(t.get('price'))
        if t.get('side') == 'Buy':
            buy += size
        else:
            sell += size
    total = buy + sell
    return buy / total if total > 0 else None


def oi_signal(oi_list: List[Dict], candles_1h: List[Candle]) -> Dict:
    """
    Изменение открытого интереса и цены за FUNNEL_OI_HOURS.
    price↑ + OI↑ — новые деньги (хорошо); price↑ + OI↓ — закрытие шортов (рост короткий).
    """
    hours = cfg.FUNNEL_OI_HOURS
    rows = sorted(oi_list or [], key=lambda r: int(_f(r.get('timestamp'))))
    if len(rows) < hours + 1 or len(candles_1h) < hours + 1:
        return {'oi_change': None, 'price_change_oi': None, 'oi_label': 'n/a'}
    oi_now, oi_then = _f(rows[-1].get('openInterest')), _f(rows[-1 - hours].get('openInterest'))
    p_now, p_then = candles_1h[-1]['close'], candles_1h[-1 - hours]['close']
    oi_ch = (oi_now - oi_then) / oi_then if oi_then else 0.0
    p_ch = (p_now - p_then) / p_then if p_then else 0.0
    if p_ch > 0 and oi_ch > 0:
        label = 'new_money'
    elif p_ch > 0 and oi_ch <= 0:
        label = 'short_covering'
    elif p_ch <= 0 and oi_ch > 0:
        label = 'shorts_building'
    else:
        label = 'longs_leaving'
    return {'oi_change': oi_ch, 'price_change_oi': p_ch, 'oi_label': label}


# ============================================================ шаг 5: оценка

@dataclass
class FunnelScore:
    score: int
    components: Dict[str, int] = field(default_factory=dict)
    vetoes: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def score_candidate(m: Dict) -> FunnelScore:
    """
    Итоговый балл по метрикам кандидата. Ожидаемые ключи m (отсутствующие = 0 баллов):
      trend_ok, pullback_ok, trigger_ok, rs_ok, book_imbalance, ask_wall,
      aggressor_share, oi_label, funding_rate, spread, long_ratio
    """
    comp: Dict[str, int] = {}
    vetoes: List[str] = []
    notes: List[str] = []

    comp['trend_4h'] = WEIGHTS['trend_4h'] if m.get('trend_ok') else 0
    comp['pullback_1h'] = WEIGHTS['pullback_1h'] if m.get('pullback_ok') else 0
    comp['trigger_15m'] = WEIGHTS['trigger_15m'] if m.get('trigger_ok') else 0
    comp['relative_strength'] = WEIGHTS['relative_strength'] if m.get('rs_ok') else 0

    imb = m.get('book_imbalance')
    if imb is not None and imb >= cfg.FUNNEL_BOOK_IMBALANCE_GOOD:
        comp['orderbook'] = WEIGHTS['orderbook']
    elif imb is not None and imb >= 1.0:
        comp['orderbook'] = WEIGHTS['orderbook'] // 2
    else:
        comp['orderbook'] = 0
    if m.get('ask_wall'):
        comp['orderbook'] -= 10
        notes.append('стена асков над ценой')

    agg = m.get('aggressor_share')
    comp['aggressor'] = WEIGHTS['aggressor'] if agg is not None and agg >= cfg.FUNNEL_AGGRESSOR_GOOD else 0

    label = m.get('oi_label')
    comp['open_interest'] = WEIGHTS['open_interest'] if label == 'new_money' else 0
    if label == 'short_covering':
        notes.append('рост на закрытии шортов')

    fr = m.get('funding_rate')
    if fr is not None:
        if fr > cfg.FUNNEL_FUNDING_VETO:
            vetoes.append(f'funding {fr*100:.3f}% > {cfg.FUNNEL_FUNDING_VETO*100:.3f}% — лонги перегреты')
            comp['funding'] = 0
        elif fr <= cfg.FUNNEL_FUNDING_GOOD_MAX:
            comp['funding'] = WEIGHTS['funding']
        else:
            comp['funding'] = 0
    else:
        comp['funding'] = 0

    lr = m.get('long_ratio')
    if lr is not None and lr > cfg.FUNNEL_LONG_RATIO_CROWDED:
        comp['crowd'] = -5
        notes.append(f'{lr*100:.0f}% аккаунтов в лонге')

    spread = m.get('spread')
    if spread is not None and spread > cfg.FUNNEL_MAX_SPREAD:
        vetoes.append(f'спред {spread*100:.2f}% > {cfg.FUNNEL_MAX_SPREAD*100:.2f}%')

    if m.get('delisting'):
        vetoes.append('объявлен делистинг')

    score = max(0, min(100, sum(comp.values())))
    return FunnelScore(score=score, components=comp, vetoes=vetoes, notes=notes)


@dataclass
class TradePlan:
    entry: float
    stop: float
    take_profit: float
    risk_per_unit: float
    stop_pct: float


def build_trade_plan(entry: float, pullback_low: float, atr_1h: float,
                     tp_r: Optional[float] = None) -> Tuple[Optional[TradePlan], str]:
    """
    Стоп — за минимумом отката (минус буфер), не ближе FUNNEL_STOP_MIN_ATR×ATR.
    Если структура требует стоп дальше FUNNEL_STOP_MAX_ATR×ATR — сделку пропускаем
    (подтягивать стоп «внутрь» структуры нельзя — это фиктивная защита).
    """
    if entry <= 0 or atr_1h <= 0:
        return None, 'нет цены или ATR'
    tp_r = tp_r if tp_r is not None else cfg.FUNNEL_TP_R
    stop = pullback_low - cfg.FUNNEL_STOP_BUFFER_ATR * atr_1h
    dist = entry - stop
    min_dist = cfg.FUNNEL_STOP_MIN_ATR * atr_1h
    max_dist = cfg.FUNNEL_STOP_MAX_ATR * atr_1h
    if dist > max_dist:
        return None, f'стоп слишком далеко ({dist/atr_1h:.1f}×ATR > {cfg.FUNNEL_STOP_MAX_ATR}×ATR)'
    if dist < min_dist:
        dist = min_dist
        stop = entry - dist
    if stop <= 0:
        return None, 'некорректный стоп'
    tp = entry + tp_r * dist
    return TradePlan(entry=entry, stop=stop, take_profit=tp, risk_per_unit=dist,
                     stop_pct=dist / entry), 'OK'


def returns_correlation(closes_a: Sequence[float], closes_b: Sequence[float]) -> Optional[float]:
    """Корреляция Пирсона лог-доходностей (по общему хвосту рядов)."""
    n = min(len(closes_a), len(closes_b))
    if n < 30:
        return None
    a, b = list(closes_a[-n:]), list(closes_b[-n:])
    ra = [math.log(a[i] / a[i - 1]) for i in range(1, n) if a[i - 1] > 0 and a[i] > 0]
    rb = [math.log(b[i] / b[i - 1]) for i in range(1, n) if b[i - 1] > 0 and b[i] > 0]
    m = min(len(ra), len(rb))
    if m < 20:
        return None
    ra, rb = ra[-m:], rb[-m:]
    try:
        return statistics.correlation(ra, rb)
    except statistics.StatisticsError:
        return None
