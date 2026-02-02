import sqlite3
import os
import sys
sys.path.insert(0, '/root/bybit_bot')

from pybit.unified_trading import HTTP
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET

def fix():
    client = HTTP(testnet=True, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
    
    # План исправлений:
    # 1. Продать SOL (66.53)
    # 2. Продать ETH (0.165)
    # 3. Купить BTC (0.001) для восстановления арбитража
    
    corrections = [
        {'coin': 'SOL', 'side': 'Sell', 'qty': 66.53},
        {'coin': 'ETH', 'side': 'Sell', 'qty': 0.165},
        {'coin': 'BTC', 'side': 'Buy', 'qty': 0.001}
    ]
    
    for c in corrections:
        pair = f"{c['coin']}USDT"
        print(f"\n⚙️ Исправление {pair}: {c['side']} {c['qty']}...")
        
        try:
            res = client.place_order(
                category="spot",
                symbol=pair,
                side=c['side'],
                orderType="Market",
                qty=str(c['qty']),
                marketUnit="baseCoin"
            )
            
            if res['retCode'] == 0:
                print(f"   ✅ Успешно! ID: {res['result']['orderId']}")
            else:
                print(f"   ❌ Ошибка: {res['retMsg']}")
        except Exception as e:
            print(f"   ❌ Исключение: {e}")

if __name__ == "__main__":
    fix()
