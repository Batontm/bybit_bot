"""
Singleton подключение к SQLite базе данных
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from config.settings import DB_PATH, logger


class DatabaseConnection:
    """Singleton для управления подключением к БД"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self.db_path = DB_PATH
        
        # Ensure database directory exists
        db_dir = Path(self.db_path).parent
        if not db_dir.exists():
            db_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"📁 Создана директория базы данных: {db_dir}")

        self._local = threading.local()
        # Initialize tables
        self.init_tables()
        self._initialized = True
        logger.info(f"🔌 DatabaseConnection инициализирован: {self.db_path}")

    def init_tables(self):
        """Инициализация таблиц базы данных"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 1. Orders
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    price REAL,
                    quantity REAL NOT NULL,
                    filled_quantity REAL DEFAULT 0,
                    avg_fill_price REAL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    is_tp INTEGER DEFAULT 0,
                    is_sl INTEGER DEFAULT 0,
                    position_id INTEGER
                )
            """)
            
            # 2. Positions
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    avg_entry_price REAL,
                    quantity REAL NOT NULL,
                    current_price REAL,
                    tp_price REAL,
                    sl_price REAL,
                    status TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    unrealized_pnl REAL DEFAULT 0,
                    unrealized_pnl_percent REAL DEFAULT 0,
                    realized_pnl REAL DEFAULT 0,
                    dca_count INTEGER DEFAULT 0,
                    breakeven_activated INTEGER DEFAULT 0
                )
            """)
            
            # 3. Trades
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    position_id INTEGER,
                    pair TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    fee REAL DEFAULT 0,
                    fee_asset TEXT DEFAULT 'USDT',
                    executed_at TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(order_id),
                    FOREIGN KEY(position_id) REFERENCES positions(id)
                )
            """)
            
            # 4. PnL History
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pnl_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    realized_pnl REAL DEFAULT 0,
                    trades_count INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    UNIQUE(date, pair)
                )
            """)
            
            # 5. LLM Requests
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS llm_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    timeframe TEXT,
                    prompt_type TEXT,
                    score REAL,
                    signal TEXT,
                    summary TEXT,
                    rejection_reason TEXT,
                    cost_usd REAL DEFAULT 0,
                    success INTEGER DEFAULT 1,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            
            # 6. Settings
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # 7. Slippage Events
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS slippage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    action TEXT,
                    slippage REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            # 8. Daily PnL (UTC)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_pnl (
                    date_utc TEXT PRIMARY KEY,
                    gross_pnl REAL DEFAULT 0,
                    net_pnl REAL DEFAULT 0,
                    commission_paid REAL DEFAULT 0,
                    slippage REAL DEFAULT 0,
                    trades_count INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)

            self._apply_migrations(conn, cursor)
            
            conn.commit()
            logger.info("💾 Таблицы базы данных проверены/созданы")
            
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации БД: {e}")
            raise
        finally:
            conn.close()

    def _apply_migrations(self, conn: sqlite3.Connection, cursor: sqlite3.Cursor) -> None:
        """Авто-миграции схемы БД (добавление недостающих колонок)."""

        def _get_columns(table: str) -> set[str]:
            cursor.execute(f"PRAGMA table_info({table})")
            return {row[1] for row in cursor.fetchall()}

        def _add_column(table: str, column: str, ddl: str) -> None:
            cols = _get_columns(table)
            if column in cols:
                return
            logger.warning(f"🛠️ DB migration: add column {table}.{column}")
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

        # orders
        _add_column("orders", "slippage", "slippage REAL DEFAULT 0")

        # positions
        _add_column("positions", "commission_paid", "commission_paid REAL DEFAULT 0")
        _add_column("positions", "slippage", "slippage REAL DEFAULT 0")
        _add_column("positions", "gross_pnl", "gross_pnl REAL DEFAULT 0")
        _add_column("positions", "net_pnl", "net_pnl REAL DEFAULT 0")
        _add_column("positions", "pyramiding_count", "pyramiding_count INTEGER DEFAULT 0")

        conn.commit()
    
    def get_connection(self):
        """Получить подключение для текущего потока"""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=10.0
            )
            self._local.connection.row_factory = sqlite3.Row
            # Включаем WAL режим для лучшей конкурентности
            self._local.connection.execute("PRAGMA journal_mode=WAL")
            logger.debug(f"📂 Создано новое подключение к БД в потоке {threading.current_thread().name}")
        
        return self._local.connection
    
    @contextmanager
    def transaction(self):
        """Контекстный менеджер для транзакций"""
        conn = self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"❌ Ошибка транзакции БД: {e}")
            raise
    
    def close(self):
        """Закрыть подключение текущего потока"""
        if hasattr(self._local, 'connection') and self._local.connection:
            self._local.connection.close()
            self._local.connection = None
            logger.debug("🔒 Подключение к БД закрыто")


# Глобальный экземпляр
db = DatabaseConnection()


@contextmanager
def get_db():
    """Хелпер для получения подключения"""
    yield db.get_connection()


@contextmanager
def get_transaction():
    """Хелпер для транзакций"""
    with db.transaction() as conn:
        yield conn
