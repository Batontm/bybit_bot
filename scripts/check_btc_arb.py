import sqlite3
import os

def check():
    db_path = "/root/bybit_bot/data/bot.db"
    if not os.path.exists(db_path):
        db_path = "data/bot.db"
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("--- АРБИТРАЖНЫЕ ПОЗИЦИИ (arbitrage_positions) ---")
    cursor.execute("SELECT pair, spot_qty, futures_qty, status FROM arbitrage_positions WHERE status = 'OPEN'")
    rows = cursor.fetchall()
    for row in rows:
        print(f"Пара: {row[0]}, Spot Qty: {row[1]}, Futures Qty: {row[2]}")
        
    print("\n--- ОБЫЧНЫЕ ПОЗИЦИИ (positions) ---")
    cursor.execute("SELECT pair, quantity, status FROM positions WHERE status = 'OPEN'")
    rows = cursor.fetchall()
    for row in rows:
        print(f"Пара: {row[0]}, Qty: {row[1]}")
        
    conn.close()

if __name__ == "__main__":
    check()
