#!/usr/bin/env python3
"""
Скрипт инициализации базы данных SQLite
Создаёт все необходимые таблицы для работы бота

Запуск: python init_database.py
"""
import sqlite3
from pathlib import Path
from datetime import datetime
import sys

# Добавляем путь к модулям (на уровень выше, т.к. скрипт в scripts/)
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import DB_PATH, TIMEZONE, logger

def create_tables(conn):
    """Создание всех таблиц БД"""
    cursor = conn.cursor()
    
    # ========== ТАБЛИЦА: bot_state ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            mode TEXT NOT NULL DEFAULT 'ACTIVE',
            auto_trading_enabled BOOLEAN NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Инициализация bot_state
    cursor.execute("""
        INSERT OR IGNORE INTO bot_state (id, mode, auto_trading_enabled)
        VALUES (1, 'ACTIVE', 1)
    """)
    
    # ========== ТАБЛИЦА: orders ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,
            pair TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            price REAL,
            quantity REAL NOT NULL,
            filled_quantity REAL DEFAULT 0,
            status TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            avg_fill_price REAL,
            is_tp BOOLEAN DEFAULT 0,
            is_sl BOOLEAN DEFAULT 0,
            position_id INTEGER,
            FOREIGN KEY (position_id) REFERENCES positions(id)
        )
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_pair ON orders(pair)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)
    """)
    
    # ========== ТАБЛИЦА: positions ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            current_price REAL,
            tp_price REAL,
            sl_price REAL,
            unrealized_pnl REAL DEFAULT 0,
            unrealized_pnl_percent REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'OPEN',
            opened_at TIMESTAMP NOT NULL,
            closed_at TIMESTAMP,
            realized_pnl REAL,
            notes TEXT
        )
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_positions_pair ON positions(pair)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)
    """)
    
    # ========== ТАБЛИЦА: trades ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT UNIQUE NOT NULL,
            order_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            quantity REAL NOT NULL,
            fee REAL DEFAULT 0,
            fee_asset TEXT,
            executed_at TIMESTAMP NOT NULL,
            position_id INTEGER,
            FOREIGN KEY (order_id) REFERENCES orders(order_id),
            FOREIGN KEY (position_id) REFERENCES positions(id)
        )
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_pair ON trades(pair)
    """)
    
    # ========== ТАБЛИЦА: pnl_history ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pnl_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            pair TEXT NOT NULL,
            realized_pnl REAL NOT NULL DEFAULT 0,
            trades_count INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            UNIQUE(date, pair)
        )
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_pnl_date ON pnl_history(date)
    """)
    
    # ========== ТАБЛИЦА: llm_requests ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS llm_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            prompt_type TEXT NOT NULL,
            score INTEGER,
            signal TEXT,
            summary TEXT,
            cost_usd REAL DEFAULT 0,
            success BOOLEAN NOT NULL DEFAULT 1,
            error_code TEXT,
            error_message TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_llm_pair ON llm_requests(pair)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_llm_created ON llm_requests(created_at)
    """)
    
    # ========== ТАБЛИЦА: market_data (опционально) ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            UNIQUE(pair, timeframe, timestamp)
        )
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_pair_tf ON market_data(pair, timeframe)
    """)
    
    # ========== ТАБЛИЦА: daily_stats ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            new_trades_count INTEGER DEFAULT 0,
            consecutive_losses INTEGER DEFAULT 0,
            daily_pnl REAL DEFAULT 0,
            reset_at TIMESTAMP
        )
    """)
    
    # ========== ТАБЛИЦА: error_log ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module TEXT NOT NULL,
            error_type TEXT NOT NULL,
            message TEXT NOT NULL,
            traceback TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_error_created ON error_log(created_at)
    """)
    
    conn.commit()
    logger.info("✅ Все таблицы созданы успешно")

def initialize_database():
    """Главная функция инициализации БД"""
    try:
        # Создаём папку data если её нет
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📁 Путь к БД: {DB_PATH}")
        
        # Подключаемся к БД
        conn = sqlite3.connect(DB_PATH)
        logger.info("🔌 Подключение к БД установлено")
        
        # Создаём таблицы
        create_tables(conn)
        
        # Проверяем созданные таблицы
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        
        logger.info(f"📊 Создано таблиц: {len(tables)}")
        for table in tables:
            logger.info(f"   - {table[0]}")
        
        conn.close()
        logger.info("✅ База данных инициализирована успешно!")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка при инициализации БД: {e}")
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ BYBIT TRADING BOT")
    print("=" * 60)
    
    success = initialize_database()
    
    if success:
        print("\n✅ Инициализация завершена успешно!")
        print(f"📁 Файл БД: {DB_PATH}")
        print("\n🎯 Следующий шаг: запустите бота командой 'python bot/main.py'")
    else:
        print("\n❌ Инициализация не удалась. Проверьте логи.")
        sys.exit(1)
