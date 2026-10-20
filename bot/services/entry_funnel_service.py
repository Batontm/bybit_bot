"""
Воронка входа — загрузка данных Bybit и прогон кандидатов через шаги entry_funnel.

Все запросы публичные (без ключей) и идут на mainnet, как и в остальных
сервисах анализа: на демо/тестнете стакан, сделки и OI нерепрезентативны.
Методы синхронные — контроллер вызывает scan() через asyncio.to_thread,
чтобы не блокировать event loop.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from pybit.unified_trading import HTTP

from config import trading_config as cfg
from config.settings import logger
from ..db.entry_signals_repo import EntrySignalsRepository
from . import entry_funnel as F
from .indicators_service import closed_candles


class EntryFunnelService:

    BTC = "BTCUSDT"

    def __init__(self, client=None) -> None:
        self.client = client or HTTP(testnet=False)
        self.repo = EntrySignalsRepository()
        self._delist_cache: Tuple[float, set] = (0.0, set())
        self._kline_cache: Dict[Tuple[str, str], Tuple[float, List[Dict]]] = {}

    # ------------------------------------------------------------- data

    def _ok(self, resp) -> bool:
        return isinstance(resp, dict) and resp.get('retCode') == 0

    def _tickers(self, category: str) -> List[Dict]:
        resp = self.client.get_tickers(category=category)
        return resp['result']['list'] if self._ok(resp) else []

    def _klines(self, symbol: str, interval: str, limit: int, ttl: float = 60.0) -> List[Dict]:
        key = (symbol, interval)
        cached = self._kline_cache.get(key)
        if cached and time.time() - cached[0] < ttl and len(cached[1]) >= limit - 1:
            return cached[1]
        resp = self.client.get_kline(category="spot", symbol=symbol, interval=interval, limit=limit)
        if not self._ok(resp):
            return []
        candles = closed_candles(resp['result']['list'])
        self._kline_cache[key] = (time.time(), candles)
        return candles

    def _orderbook(self, symbol: str) -> Tuple[List, List]:
        resp = self.client.get_orderbook(category="spot", symbol=symbol, limit=50)
        if not self._ok(resp):
            return [], []
        return resp['result'].get('b', []), resp['result'].get('a', [])

    def _trades(self, symbol: str, has_perp: bool) -> List[Dict]:
        # спот отдаёт максимум 60 сделок, у перпа — до 1000: для окна 15 минут берём перп
        category, limit = ("linear", 1000) if has_perp else ("spot", 60)
        resp = self.client.get_public_trade_history(category=category, symbol=symbol, limit=limit)
        return resp['result']['list'] if self._ok(resp) else []

    def _open_interest(self, symbol: str) -> List[Dict]:
        resp = self.client.get_open_interest(category="linear", symbol=symbol,
                                             intervalTime="1h", limit=cfg.FUNNEL_OI_HOURS + 2)
        return resp['result']['list'] if self._ok(resp) else []

    def _long_ratio(self, symbol: str) -> Optional[float]:
        try:
            resp = self.client.get_long_short_ratio(category="linear", symbol=symbol, period="1h", limit=1)
            if self._ok(resp) and resp['result']['list']:
                return float(resp['result']['list'][0]['buyRatio'])
        except Exception as e:
            logger.debug(f"long/short ratio {symbol}: {e}")
        return None

    def _delisted_bases(self) -> set:
        """Монеты из анонсов Bybit о делистинге за последние 30 дней (кэш 1 час)."""
        ts, bases = self._delist_cache
        if time.time() - ts < 3600:
            return bases
        bases = set()
        getter = getattr(self.client, 'get_announcement', None)
        if getter is not None:
            try:
                resp = getter(locale="en-US", type="delistings", limit=50)
                if self._ok(resp):
                    cutoff = (time.time() - 30 * 86400) * 1000
                    for item in resp['result'].get('list', []):
                        if float(item.get('dateTimestamp') or 0) < cutoff:
                            continue
                        title = item.get('title', '')
                        bases.update(re.findall(r'\b([A-Z0-9]{2,15})(?:USDT|/USDT)?\b', title))
            except Exception as e:
                logger.debug(f"announcements: {e}")
        self._delist_cache = (time.time(), bases)
        return bases

    # ------------------------------------------------------------- scan

    def scan(self, *, exclude: Sequence[str] = (), always_include: Sequence[str] = ()) -> List[Dict]:
        """
        Прогнать отбор и вернуть кандидатов, прошедших все обязательные шаги,
        отсортированных по баллу. Каждый кандидат — dict:
          pair, score, components, vetoes, notes, metrics, plan (TradePlan), signal_id
        В итоговый список попадают только кандидаты со score >= FUNNEL_MIN_SCORE и без вето.
        """
        t0 = time.time()
        try:
            spot = self._tickers("spot")
            linear = self._tickers("linear")  # нужен всегда: funding и наличие перпа
        except Exception as e:
            logger.error(f"❌ Воронка: не удалось получить тикеры: {e}")
            return []

        universe = F.select_universe(spot, linear, always_include=always_include, exclude=exclude)
        if not universe:
            logger.info("ℹ️ Воронка: ни одна пара не прошла отбор по ликвидности/спреду")
            return []

        btc_ticker = next((t for t in spot if t.get('symbol') == self.BTC), {})
        btc_change = F._f(btc_ticker.get('price24hPcnt')) * 100
        btc_4h = self._klines(self.BTC, "240", 120, ttl=300)
        delisted = self._delisted_bases()

        candidates: List[Dict] = []
        deep_checks = 0
        stats = {'universe': len(universe), 'trend': 0, 'trigger': 0}

        for item in universe:
            pair = item['symbol']
            try:
                result = self._evaluate(item, btc_4h, btc_change, delisted,
                                        allow_deep=deep_checks < cfg.FUNNEL_MAX_DEEP_CHECKS)
            except Exception as e:
                logger.debug(f"Воронка {pair}: ошибка {e}")
                continue
            if result is None:
                continue
            stats['trend'] += 1  # None возвращается только если тренд 4h не прошёл
            if result['stage'] == 'scored':
                stats['trigger'] += 1
                deep_checks += 1
            if result.get('passed'):
                candidates.append(result)

        candidates.sort(key=lambda c: c['score'], reverse=True)
        logger.info(
            f"🧭 Воронка: отбор {stats['universe']} → тренд 4h {stats['trend']} → "
            f"триггер 15m {stats['trigger']} → проходят по баллу {len(candidates)} "
            f"({time.time() - t0:.1f}с)"
        )
        for c in candidates[:3]:
            logger.info(f"   🎯 {c['pair']}: {c['score']} баллов {c['components']}")
        return candidates

    def _evaluate(self, item: Dict, btc_4h: List[Dict], btc_change: float,
                  delisted: set, allow_deep: bool) -> Optional[Dict]:
        pair = item['symbol']
        metrics: Dict = {'spread': item['spread'], 'change_24h': item['change_24h'],
                         'funding_rate': item['funding_rate'], 'turnover': item['turnover']}

        # Шаг 2a — тренд 4h (обязательный)
        c4h = self._klines(pair, "240", 120, ttl=300)
        trend_ok, trend_info = F.check_trend_4h(c4h)
        metrics.update(trend_info)
        if not trend_ok:
            return None  # не логируем: таких пар большинство

        # Шаг 2b — откат 1h и сила к BTC (баллы)
        c1h = self._klines(pair, "60", 200, ttl=120)
        pullback_ok, pb_info = F.check_pullback_1h(c1h)
        metrics.update(pb_info)
        rs_ok, rs_info = F.relative_strength(c4h, btc_4h, item['change_24h'], btc_change)
        metrics.update(rs_info)
        metrics.update({'trend_ok': True, 'pullback_ok': pullback_ok, 'rs_ok': rs_ok})

        if 'atr_1h' not in metrics:
            self._log(pair, 'trend', metrics, None, 'мало данных 1h')
            return {'pair': pair, 'stage': 'trend', 'passed': False}

        # Шаг 3 — триггер 15m (обязательный)
        c15 = self._klines(pair, cfg.FUNNEL_TRIGGER_TF, 60, ttl=30)
        trig_ok, trig_info = F.check_trigger_15m(c15, item['price'])
        metrics.update(trig_info)
        metrics['trigger_ok'] = trig_ok
        if not trig_ok:
            self._log(pair, 'trigger', metrics, None, trig_info.get('reason', 'нет триггера'))
            return {'pair': pair, 'stage': 'trigger', 'passed': False}

        if not allow_deep:
            self._log(pair, 'trigger', metrics, None, 'лимит глубоких проверок за цикл')
            return {'pair': pair, 'stage': 'trigger', 'passed': False}

        # Шаг 4 — поток ордеров и деривативы
        bids, asks = self._orderbook(pair)
        metrics.update(F.orderbook_metrics(bids, asks, item['price']))
        metrics['aggressor_share'] = F.aggressor_share(self._trades(pair, item['has_perp']),
                                                       int(time.time() * 1000))
        if item['has_perp']:
            metrics.update(F.oi_signal(self._open_interest(pair), c1h))
            metrics['long_ratio'] = self._long_ratio(pair)
        metrics['delisting'] = pair[:-4] in delisted

        # Шаг 5 — оценка и план сделки
        fs = F.score_candidate(metrics)
        plan, plan_reason = F.build_trade_plan(item['price'], metrics['pullback_low'], metrics['atr_1h'])

        reasons = list(fs.vetoes)
        if fs.score < cfg.FUNNEL_MIN_SCORE:
            reasons.append(f'балл {fs.score} < {cfg.FUNNEL_MIN_SCORE}')
        if plan is None:
            reasons.append(plan_reason)
        passed = not reasons

        signal_id = self._log(
            pair, 'scored', metrics, fs, '; '.join(reasons) if reasons else 'кандидат',
            decision='CANDIDATE' if passed else 'SKIP',
            price=item['price'],
            stop=plan.stop if plan else None, tp=plan.take_profit if plan else None,
        )
        return {
            'pair': pair, 'stage': 'scored', 'passed': passed, 'score': fs.score,
            'components': fs.components, 'vetoes': fs.vetoes, 'notes': fs.notes,
            'metrics': metrics, 'plan': plan, 'signal_id': signal_id,
        }

    def _log(self, pair: str, stage: str, metrics: Dict, fs: Optional[F.FunnelScore],
             reason: str, decision: str = 'SKIP', price=None, stop=None, tp=None) -> Optional[int]:
        if not cfg.FUNNEL_LOG_SIGNALS:
            return None
        return self.repo.log(
            pair, stage, score=fs.score if fs else None,
            components=fs.components if fs else None, metrics=metrics,
            vetoes=fs.vetoes if fs else None, decision=decision, reason=reason,
            price=price, stop_price=stop, tp_price=tp,
        )

    # ------------------------------------------------------------- correlation

    def correlation_block(self, pair: str, open_pairs: Sequence[str]) -> Tuple[bool, str]:
        """Не открывать вторую позицию в группе пар, которые ходят вместе (corr > FUNNEL_CORR_MAX)."""
        others = [p for p in set(open_pairs) if p != pair]
        if not others:
            return False, ''
        base = self._klines(pair, "60", 200, ttl=600)
        closes = [c['close'] for c in base][-168:]
        for other in others:
            oc = [c['close'] for c in self._klines(other, "60", 200, ttl=600)][-168:]
            corr = F.returns_correlation(closes, oc)
            if corr is not None and corr > cfg.FUNNEL_CORR_MAX:
                return True, f'корреляция с {other} = {corr:.2f} > {cfg.FUNNEL_CORR_MAX}'
        return False, ''


entry_funnel_service = EntryFunnelService()
