#!/usr/bin/env python3
"""Проверка pnl_history и закрытых позиций"""
import sys
sys.path.insert(0, '/root/bybit_bot')
import sqlite3

conn = sqlite3.connect('/root/bybit_bot/data/bot.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

print('=== PNL_HISTORY ===')
cursor.execute('SELECT COUNT(*) as cnt FROM pnl_history')
print(f"Записей: {cursor.fetchone()['cnt']}")

cursor.execute('SELECT * FROM pnl_history LIMIT 5')
for row in cursor.fetchall():
    print(dict(row))

print()
print('=== ЗАКРЫТЫЕ ПОЗИЦИИ ===')
cursor.execute("SELECT id, pair, realized_pnl, status, closed_at FROM positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT 10")
rows = cursor.fetchall()
if not rows:
    print("Нет закрытых позиций")
else:
    for row in rows:
        print(dict(row))

print()
print('=== ОТКРЫТЫЕ ПОЗИЦИИ ===')
cursor.execute("SELECT id, pair, unrealized_pnl, status FROM positions WHERE status='OPEN'")
rows = cursor.fetchall()
if not rows:
    print("Нет открытых позиций")
else:
    for row in rows:
        print(dict(row))

conn.close()
