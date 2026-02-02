"""
Конфигурация API ключей и эндпоинтов для Bybit и Perplexity
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ============= BYBIT API =============
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api-testnet.bybit.com")
BYBIT_TESTNET = "testnet" in BYBIT_BASE_URL  # True если используем тестнет

BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED")

# WebSocket эндпоинты для тестнета
BYBIT_WS_PUBLIC = "wss://stream-testnet.bybit.com/v5/public/spot"
BYBIT_WS_PRIVATE = "wss://stream-testnet.bybit.com/v5/private"

# REST эндпоинты
BYBIT_ENDPOINTS = {
    "balance": "/v5/account/wallet-balance",
    "place_order": "/v5/order/create",
    "cancel_order": "/v5/order/cancel",
    "open_orders": "/v5/order/realtime",
    "order_history": "/v5/order/history",
    "kline": "/v5/market/kline",
    "tickers": "/v5/market/tickers",
    "instruments": "/v5/market/instruments-info",
}

# ============= PERPLEXITY API =============
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")
PERPLEXITY_BASE_URL = "https://api.perplexity.ai"
# Актуальные модели: sonar, sonar-pro, sonar-reasoning
PERPLEXITY_MODEL = "sonar"

# ============= OLLAMA (локальная LLM) =============
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")

# ============= TELEGRAM API =============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============= ВАЛИДАЦИЯ =============
def validate_config():
    """Проверка наличия обязательных ключей"""
    errors = []
    
    if not BYBIT_API_KEY:
        errors.append("BYBIT_API_KEY не установлен")
    if not BYBIT_API_SECRET:
        errors.append("BYBIT_API_SECRET не установлен")
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN не установлен")
    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID не установлен")
    
    if not PERPLEXITY_API_KEY:
        errors.append("⚠️  PERPLEXITY_API_KEY не установлен (бот будет в режиме RISK_ONLY)")
    
    return errors

if __name__ == "__main__":
    errors = validate_config()
    if errors:
        print("❌ Ошибки конфигурации:")
        for error in errors:
            print(f"  - {error}")
    else:
        print("✅ Все ключи API настроены корректно")
