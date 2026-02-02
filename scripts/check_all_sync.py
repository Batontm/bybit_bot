import sqlite3
import os
import sys
sys.path.insert(0, '/root/bybit_bot')

from pybit.unified_trading import HTTP
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET

def check():
    client = HTTP(testnet=True, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
    
    # 1. Сбор данных из Bybit SPOT
    print("--- BYBIT SPOT ---")
    wallet = client.get_wallet_balance(accountType="UNIFIED")
    spot_balances = {}
    if wallet['retCode'] == 0:
        for coin in wallet['result']['list'][0].get('coin', []):
            bal = float(coin.get('walletBalance', 0))
            if bal > 0:
                spot_balances[coin['coin']] = bal
                print(f"{coin['coin']}: {bal}")
    
    # 2. Сбор данных из Bybit LINEAR (Futures)
    print("\n--- BYBIT FUTURES ---")
    futures = client.get_positions(category="linear", settleCoin="USDT")
    futures_pos = {}
    if futures['retCode'] == 0:
        for p in futures['result']['list']:
            size = float(p.get('size', 0))
            if size > 0:
                futures_pos[p['symbol']] = {'size': size, 'side': p['side']}
                print(f"{p['symbol']}: {p['side']} {size}")

    # 3. Сбор данных из DB
    print("\n--- DATABASE ---")
    conn = sqlite3.connect("/root/bybit_bot/data/bot.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    cur.execute("SELECT pair, quantity FROM positions WHERE status='OPEN'")
    db_positions = {r['pair']: r['quantity'] for r in cur.fetchall()}
    print(f"Активные позиции Spot: {db_positions}")
    
    cur.execute("SELECT pair, spot_qty, futures_qty FROM arbitrage_positions WHERE status='OPEN'")
    db_arbitrage = {r['pair']: {'spot': r['spot_qty'], 'futures': r['futures_qty']} for r in cur.fetchall()}
    print(f"Активный Арбитраж: {db_arbitrage}")
    
    conn.close()
    
    # 4. Анализ расхождений
    print("\n--- АНАЛИЗ РАСХОЖДЕНИЙ ---")
    for coin, bal in spot_balances.items():
        if coin == 'USDT' or coin == 'USDC': continue
        
        pair = f"{coin}USDT"
        expected = db_positions.get(pair, 0) + db_arbitrage.get(pair, {}).get('spot', 0)
        
        diff = bal - expected
        if abs(diff) > 0.000001:
            print(f"⚠️ {coin}: Баланс {bal}, ожидалось {expected} (Разница: {diff})")
        else:
            print(f"✅ {coin}: Баланс совпадает ({bal})")

if __name__ == "__main__":
    check()
