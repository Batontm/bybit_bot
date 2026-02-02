import sqlite3
import os

db_path = "/root/bybit_bot/data/bot.db"
if not os.path.exists(db_path):
    db_path = "data/bot.db"

def check_db():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    print("=== АКТИВНЫЕ ПОЗИЦИИ (positions) ===")
    try:
        cur.execute("SELECT pair, status, entry_price, quantity FROM positions WHERE status='OPEN'")
        rows = cur.fetchall()
        for r in rows:
            print(dict(r))
        if not rows: print("Нет")
    except Exception as e:
        print(f"Ошибка positions: {e}")
        
    print("\n=== АКТИВНЫЙ АРБИТРАЖ (arbitrage_positions) ===")
    try:
        cur.execute("SELECT pair, status, spot_qty, futures_qty FROM arbitrage_positions WHERE status='OPEN'")
        rows = cur.fetchall()
        for r in rows:
            print(dict(r))
        if not rows: print("Нет")
    except Exception as e:
        print(f"Ошибка arbitrage_positions: {e}")
        
    print("\n=== ПОСЛЕДНИЕ 5 ЗАКРЫТЫХ ПОЗИЦИЙ ===")
    try:
        cur.execute("SELECT pair, status, realized_pnl, closed_at FROM positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT 5")
        rows = cur.fetchall()
        for r in rows:
            print(dict(r))
    except Exception as e:
        print(f"Ошибка closed positions: {e}")
        
    conn.close()

if __name__ == "__main__":
    check_db()
