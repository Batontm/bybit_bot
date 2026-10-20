"""
Проверка API-ключей Bybit из .env (без сторонних библиотек — только stdlib).

Запуск из корня проекта:
    python3 scripts/check_api_keys.py

Что делает:
  1. Пробует ключи на трёх средах Bybit: demo, testnet, mainnet — ключи
     действуют только в той среде, где созданы.
  2. Для рабочей среды показывает права ключа (/v5/user/query-api):
     только чтение или торговля, разрешён ли вывод, привязка к IP, срок действия.
  3. Показывает баланс единого торгового аккаунта (UNIFIED).
  4. Сравнивает с BYBIT_BASE_URL в .env и подсказывает, что поменять.

Ключи на экран не выводятся.
"""
import hashlib
import hmac
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENVS = {
    "demo": "https://api-demo.bybit.com",
    "testnet": "https://api-testnet.bybit.com",
    "mainnet": "https://api.bybit.com",
}
RECV_WINDOW = "10000"


def _ssl_context() -> ssl.SSLContext:
    """
    Python с python.org на macOS ставится без корневых сертификатов
    (CERTIFICATE_VERIFY_FAILED). Если установлен certifi — берём его набор,
    проверка сертификата при этом остаётся включённой.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()
SSL_HINT = (
    "\n💡 Python на Mac не видит корневые сертификаты. Исправить один раз:\n"
    "   open \"/Applications/Python 3.$(python3 -c 'import sys;print(sys.version_info[1])')/Install Certificates.command\"\n"
    "   или: python3 -m pip install --upgrade certifi\n"
    "   и запустить проверку снова."
)


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def signed_get(base: str, path: str, params: dict, key: str, secret: str) -> dict:
    query = urllib.parse.urlencode(params)
    ts = str(int(time.time() * 1000))
    sign = hmac.new(secret.encode(), (ts + key + RECV_WINDOW + query).encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{base}{path}?{query}" if query else f"{base}{path}",
        headers={
            "X-BAPI-API-KEY": key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"retCode": e.code, "retMsg": f"HTTP {e.code}"}
    except Exception as e:
        return {"retCode": -1, "retMsg": f"нет соединения: {e}"}


def main() -> int:
    env = load_env(ROOT / ".env")
    key = env.get("BYBIT_API_KEY") or os.getenv("BYBIT_API_KEY", "")
    secret = env.get("BYBIT_API_SECRET") or os.getenv("BYBIT_API_SECRET", "")
    configured = env.get("BYBIT_BASE_URL") or os.getenv("BYBIT_BASE_URL", "https://api-demo.bybit.com")

    if not key or not secret:
        print("❌ В .env нет BYBIT_API_KEY / BYBIT_API_SECRET")
        return 1

    print(f"🔑 Ключ: {key[:4]}…{key[-2:]} | в .env указана среда: {configured}\n")

    working = None
    offline = 0
    ssl_failed = False
    for name, base in ENVS.items():
        r = signed_get(base, "/v5/user/query-api", {}, key, secret)
        code, msg = r.get("retCode"), r.get("retMsg")
        if code == 0:
            print(f"✅ {name:8} {base} — ключ принят")
            working = working or (name, base, r.get("result") or {})
        elif code == 10003:
            print(f"➖ {name:8} {base} — ключ не из этой среды")
        elif code == 10004:
            print(f"❌ {name:8} {base} — неверная подпись: секретный ключ не подходит к API-ключу")
        elif code == 10010:
            print(f"⛔ {name:8} {base} — IP этого компьютера не в белом списке ключа")
        elif code == -1:
            offline += 1
            ssl_failed = ssl_failed or 'CERTIFICATE_VERIFY_FAILED' in str(msg)
            print(f"⚠️  {name:8} {base} — {msg}")
        else:
            print(f"❌ {name:8} {base} — retCode={code}: {msg}")

    if not working:
        if ssl_failed:
            print("\n⚠️ Соединение не установлено из-за сертификатов Python — ключи не проверены.")
            print(SSL_HINT)
        elif offline == len(ENVS):
            print("\n⚠️ Bybit недоступен с этого компьютера (сеть/VPN/блокировка) — ключи не проверены.")
        else:
            print("\n❌ Ключи не приняты ни одной средой Bybit.")
        return 1

    name, base, info = working
    perms = info.get("permissions") or {}
    print(f"\n📋 Права ключа ({name}):")
    print(f"   Только чтение: {'да ❌ (бот не сможет торговать)' if info.get('readOnly') == 1 else 'нет ✅'}")
    print(f"   Спот: {perms.get('Spot') or '—'}")
    print(f"   Деривативы: {perms.get('ContractTrade') or perms.get('Derivatives') or '—'}")
    wallet = perms.get('Wallet') or []
    print(f"   Кошелёк: {wallet or '—'}{'  ⚠️ разрешён вывод — для бота лучше выключить' if 'Withdraw' in wallet else ''}")
    ips = info.get('ips') or []
    print(f"   Привязка к IP: {', '.join(ips) if ips and ips != ['*'] else 'нет (ключ работает с любого IP)'}")
    if info.get('expiredAt'):
        print(f"   Действует до: {info.get('expiredAt')}")

    bal = signed_get(base, "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, key, secret)
    if bal.get("retCode") == 0 and bal["result"].get("list"):
        acc = bal["result"]["list"][0]
        print(f"\n💰 Баланс UNIFIED: equity ${float(acc.get('totalEquity') or 0):,.2f}")
        for c in acc.get("coin", []):
            if float(c.get("walletBalance") or 0) > 0:
                print(f"   {c['coin']}: {c['walletBalance']}")
    else:
        print(f"\n⚠️ Баланс UNIFIED не получен: {bal.get('retMsg')}")

    if configured.rstrip('/') != base:
        print(f"\n👉 В .env BYBIT_BASE_URL={configured}, а ключи из среды {name}. Поменяй на:\n   BYBIT_BASE_URL={base}")
    else:
        print("\n✅ BYBIT_BASE_URL в .env совпадает со средой ключей.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
