"""
Основные настройки бота - чтение .env, таймзона, режимы работы
"""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv
import pytz

# Загружаем .env из корня проекта
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ============= ОСНОВНЫЕ НАСТРОЙКИ =============

# Таймзона
TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Europe/Kaliningrad"))

# Режим работы
TESTNET = os.getenv("TESTNET", "True").lower() == "true"
AUTO_TRADING_ENABLED = os.getenv("AUTO_TRADING_ENABLED", "True").lower() == "true"

# Логирование
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = BASE_DIR / "logs" / "bot.log"

# База данных
DB_PATH = BASE_DIR / "data" / "bot.db"

# ============= РЕЖИМЫ БОТА =============
class BotMode:
    ACTIVE = "ACTIVE"           # Нормальная работа
    RISK_ONLY = "RISK_ONLY"     # Без ИИ
    PAUSED = "PAUSED"           # На паузе

# ============= НАСТРОЙКА ЛОГГЕРА =============
def setup_logger():
    """Настройка логгера с форматированием"""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger("bybit_bot")
    logger.setLevel(logging.DEBUG)  # Глобально разрешаем все уровни, фильтруем в хендлерах
    
    # Если хендлеры уже есть, не добавляем новые
    if logger.hasHandlers():
        return logger
    
    # Формат лога
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # File handler - пишет всё (DEBUG и выше)
    fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    
    # Console handler - пишет только то, что настроено в .env (по умолчанию INFO)
    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, LOG_LEVEL))
    ch.setFormatter(formatter)
    
    logger.addHandler(fh)
    logger.addHandler(ch)
    
    return logger

# ============= ЭКСПОРТ =============
logger = setup_logger()
