#!/usr/bin/env python3
"""Проверка балансов и открытых ордеров"""
import sys
sys.path.insert(0, '/root/bybit_bot')

from pybit.unified_trading import HTTP
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET

client = HTTP(testnet=True, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)

print("=== ОТКРЫТЫЕ ОРДЕРА ===")
for pair in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']:
    orders = client.get_open_orders(category='spot', symbol=pair)
    if orders['result']['list']:
        for o in orders['result']['list']:
            print(f"{pair}: {o['orderId']} | {o['side']} | qty={o['qty']}")
    else:
        print(f"{pair}: нет открытых ордеров")

print()
print("=== ДЕТАЛЬНЫЕ БАЛАНСЫ ===")
wallet = client.get_wallet_balance(accountType='UNIFIED')
for coin in wallet['result']['list'][0]['coin']:
    balance = float(coin.get('walletBalance', 0))
    if balance > 0:
        free = coin.get('availableToWithdraw', 'N/A')
        locked = coin.get('locked', 'N/A')
        print(f"{coin['coin']}: total={balance}, free={free}, locked={locked}")
