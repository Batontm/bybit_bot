"""
Все экраны Telegram на тестовых данных:
- HTML допустим для Telegram (только b/i/u/s/code/pre/a, теги закрыты, «<» экранирован),
- нет битых символов (U+FFFD) и «$-» вместо «-$»,
- цены дешёвых монет не превращаются в $0.00,
- у каждой кнопки есть обработчик.
"""
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest

from bot.telegram_aiogram import handlers as H
from bot.services.market_regime_service import RegimeStatus

ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler"}


class _TgHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.errors.append(f"тег <{tag}> Telegram не поддерживает")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"лишний/неверный </{tag}>")
        else:
            self.stack.pop()


def assert_tg_html(text: str):
    assert "�" not in text, "битый символ U+FFFD"
    assert "$-" not in text and "+$-" not in text, f"знак после $: {text}"
    p = _TgHTML()
    p.feed(text)
    p.close()
    assert not p.errors and not p.stack, (p.errors, p.stack, text)


# ----------------------------------------------------------------- фейковые данные

class FakeBalance:
    last_total_equity = 153_300.0

    def get_all_balances(self):
        return [
            {'coin': 'USDT', 'total': 150_000.0, 'free': 149_000.0, 'usd_value': 150_000.0},
            {'coin': 'ETH', 'total': 1.2, 'free': 0.0, 'usd_value': 3_300.0},
            {'coin': 'BTC', 'total': 0.00000007, 'free': 0.00000007, 'usd_value': 0.004},
        ]

    def get_deposit_usdt(self):
        return 150_000.0


POSITIONS = [
    {'id': 1, 'pair': 'ETHUSDT', 'entry_price': 2750.0, 'current_price': 2790.5, 'quantity': 1.2,
     'sl_price': 2700.0, 'tp_price': 2875.0},
    {'id': 2, 'pair': 'PEPEUSDT', 'entry_price': 0.00001234, 'current_price': 0.00001201,
     'quantity': 10_000_000, 'sl_price': 0.0000118, 'tp_price': 0.0000135},
]


class FakeArb:
    MIN_FUNDING_RATE = 0.0001

    def get_dashboard(self):
        return {'stats': {'open_positions': 1, 'closed_positions': 2, 'open_funding': -1.5, 'total_pnl': 3.25},
                'positions': [{'pair': 'BTCUSDT', 'spot_qty': 0.01, 'entry_price': 67000, 'accumulated_funding': -1.5}]}

    def scan_funding_rates(self):
        return [{'pair': 'ETHUSDT', 'risk': '🟢', 'funding_pct': 0.012, 'apy': 13.1, 'mark_price': 2790.1,
                 'funding_rate': 0.00012},
                {'pair': 'X<Y>USDT', 'risk': '🔴', 'funding_pct': 0.001, 'apy': 1.0, 'mark_price': 0.0004321,
                 'funding_rate': 0.00001}]


class FakeRegime:
    RSI_MIN = 40
    ATR_SPIKE_MULTIPLIER = 2.0

    def __init__(self, allowed=True):
        self.allowed = allowed

    def get_status(self, **kw):
        return RegimeStatus(allowed=self.allowed, label='', checked_at=datetime.now(timezone.utc),
                            reason='Режим КРАСНЫЙ: close <= EMA200' if not self.allowed else 'ok',
                            price=67000.0, ema200_4h=65000.0, rsi14_4h=45.0, atr_ratio=1.1)


class FakeController:
    started_at = datetime.now(timezone.utc)
    is_running = True

    def __init__(self, mode='ACTIVE'):
        self.mode = mode

    def get_mode(self):
        return self.mode

    def is_auto_trading_enabled(self):
        return True

    def is_scanner_enabled(self):
        return False

    def is_notifications_enabled(self):
        return True

    def is_alerts_enabled(self):
        return False


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(H, 'balance_service', FakeBalance())
    monkeypatch.setattr(H, 'arbitrage_service', FakeArb())
    monkeypatch.setattr(H, 'market_regime_service', FakeRegime())
    monkeypatch.setattr(H.TradesRepository, 'get_open_positions', staticmethod(lambda pair=None: POSITIONS))
    monkeypatch.setattr(H.TradesRepository, 'get_closed_positions_between_utc',
                        staticmethod(lambda a, b: [{'net_pnl': 12.5}, {'net_pnl': -4.0}]))
    monkeypatch.setattr(H.DailyPnLRepository, 'get_last_days', staticmethod(lambda n: [
        {'date_utc': '2026-10-20', 'net_pnl': -3.5, 'trades_count': 2, 'wins': 1, 'commission_paid': 0.4, 'slippage': 0.001},
        {'date_utc': '2026-10-18', 'net_pnl': 10.0, 'trades_count': 3, 'wins': 2, 'commission_paid': 0.6, 'slippage': 0.0},
    ]))


# ----------------------------------------------------------------- экраны

def test_dashboard(fakes):
    t = H._format_health(FakeController())
    assert_tg_html(t)
    assert "$153,300.00" in t                    # капитал в долларах, а не сумма монет
    assert "ETH" in t and "≈ $3,300.00" in t
    assert "мелкие остатки" in t                  # пыль BTC свёрнута
    assert "$0.00001201" in t                     # цена PEPE не стала $0.00
    assert "-$" in t and "+$" in t                # PnL позиций с правильным знаком
    assert "Риск до стопов" in t
    assert "Светофор" in t


def test_regime_uses_service_thresholds(fakes):
    t = H._format_regime()
    assert_tg_html(t)
    assert "нужно ≥ 40" in t and "✅" in t         # RSI 45 при пороге 40 — зелёная галочка
    assert "&gt; 50" not in t


def test_regime_red(fakes, monkeypatch):
    monkeypatch.setattr(H, 'market_regime_service', FakeRegime(allowed=False))
    assert_tg_html(H._format_regime())


def test_pnl_screens(fakes):
    for days in (7, 0):
        t = H._format_pnl_summary(days)
        assert_tg_html(t)
        assert t.index('2026-10-18') < t.index('2026-10-20')   # дни по порядку
        assert "Сегодня (UTC): <b>+$8.50</b>" in t


def test_arbitrage_screens(fakes):
    t = H._format_arbitrage_dashboard()
    assert_tg_html(t)
    assert "-$1.50" in t and "+$-" not in t
    text, opps = H._format_arbitrage_analysis(10, position_size=100)
    assert_tg_html(text)
    assert "X&lt;Y&gt;USDT" in text                # имена экранируются
    assert_tg_html(H._format_arbitrage_pnl_summary())


def test_settings_screen_and_pause(fakes):
    for mode in ('ACTIVE', 'PAUSED'):
        c = FakeController(mode)
        assert_tg_html(H._format_settings_text(c))
    assert "пауза" in H._format_settings_text(FakeController('PAUSED'))


def test_positions_empty(fakes, monkeypatch):
    monkeypatch.setattr(H.TradesRepository, 'get_open_positions', staticmethod(lambda pair=None: []))
    t = H._format_open_positions_block()
    assert_tg_html(t)
    assert "Нет открытых" in t


@pytest.mark.parametrize("v,expected", [
    (67123.456, "$67,123.46"), (2.5, "$2.5"), (0.1234, "$0.1234"),
    (0.00001234, "$0.00001234"), (0, "$0"),
])
def test_price_format(v, expected):
    assert H._price(v) == expected


def test_usd_sign():
    assert H._usd(-1.5, sign=True) == "-$1.50"
    assert H._usd(2, sign=True) == "+$2.00"
    assert H._usd(1234.5) == "$1,234.50"


# ----------------------------------------------------------------- кнопки

def _handled_callbacks(src: str):
    exact = set(re.findall(r'data == "(tg:[^"]+)"', src))
    for group in re.findall(r'data in \(([^)]*)\)', src):
        exact |= set(re.findall(r'"(tg:[^"]+)"', group))
    prefixes = set(re.findall(r'data\.startswith\("(tg:[^"]+)"\)', src))
    return exact, prefixes


def test_every_button_has_handler(fakes):
    src = Path(H.__file__).read_text(encoding='utf-8')
    bot_src = Path(H.__file__).with_name('bot.py').read_text(encoding='utf-8')
    exact, prefixes = _handled_callbacks(src)
    datas = set(re.findall(r'callback_data=f?"(tg:[^"{]+)', src + bot_src))
    missing = [d for d in datas if d not in exact and not any(d.startswith(p) for p in prefixes)]
    assert not missing, f"кнопки без обработчика: {missing}"
