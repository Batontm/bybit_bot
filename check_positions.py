#!/usr/bin/env python3
"""Скрипт для проверки позиций и ордеров"""
import sqlite3
import sys
sys.path.insert(0, '/root/bybit_bot')

from pybit.unified_trading import HTTP
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET

# Подключение к БД
conn = sqlite3.connect('/root/bybit_bot/data/bot.db')
conn.row_factory = sqlite3.Row

print("=" * 60)
print("ОТКРЫТЫЕ ПОЗИЦИИ В БД:")
print("=" * 60)

cursor = conn.cursor()
cursor.execute("SELECT * FROM positions WHERE status = 'OPEN'")
positions = cursor.fetchall()

for pos in positions:
    print(f"\nID: {pos['id']}")
    print(f"  Пара: {pos['pair']}")
    print(f"  Entry: {pos['entry_price']}")
    print(f"  Qty: {pos['quantity']}")
    print(f"  TP: {pos['tp_price']}")
    print(f"  SL: {pos['sl_price']}")

print("\n" + "=" * 60)
print("ОРДЕРА ДЛЯ ПОЗИЦИЙ В БД:")
print("=" * 60)

for pos in positions:
    cursor.execute("""
        SELECT order_id, pair, side, order_type, price, is_tp, is_sl, status 
        FROM orders 
        WHERE position_id = ?
    """, (pos['id'],))
    orders = cursor.fetchall()
    
    print(f"\nОрдера для {pos['pair']} (position_id={pos['id']}):")
    if not orders:
        print("  [НЕТ ОРДЕРОВ В БД]")
    for order in orders:
        tp_sl = "TP" if order['is_tp'] else ("SL" if order['is_sl'] else "")
        print(f"  {order['order_id'][:20]}... | {order['side']} | {order['order_type']} | {tp_sl} | {order['status']}")

print("\n" + "=" * 60)
print("АКТИВНЫЕ ОРДЕРА НА BYBIT:")
print("=" * 60)

client = HTTP(
    testnet=True,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET
)

for pos in positions:
    pair = pos['pair']
    try:
        # Открытые ордера
        open_orders = client.get_open_orders(category="spot", symbol=pair)
        orders_list = open_orders.get('result', {}).get('list', [])
        
        print(f"\n{pair} - открытые ордера на бирже:")
        if not orders_list:
            print("  [НЕТ ОТКРЫТЫХ ОРДЕРОВ]")
        for o in orders_list:
            print(f"  {o['orderId'][:20]}... | {o['side']} | {o['orderType']} | qty={o['qty']} | triggerPrice={o.get('triggerPrice', '-')}")
    except Exception as e:
        print(f"  Ошибка: {e}")

conn.close()
