"""
Конфигурация API ключей и эндпоинтов для Bybit и Perplexity
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ============= BYBIT API =============
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api-demo.bybit.com")
BYBIT_TESTNET = "testnet" in BYBIT_BASE_URL  # True если используем тестнет
BYBIT_DEMO = "demo" in BYBIT_BASE_URL  # True если используем демо-торговлю

BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED")

def get_pybit_kwargs() -> dict:
    """Возвращает kwargs для pybit HTTP/WebSocket в зависимости от режима (demo/testnet/prod)"""
    if BYBIT_DEMO:
        return {"demo": True, "testnet": False}
    elif BYBIT_TESTNET:
        return {"testnet": True}
    else:
        return {"testnet": False}

def get_pybit_ws_public_kwargs() -> dict:
    """Kwargs для публичного WebSocket (демо не поддерживает public streams — используем mainnet)"""
    if BYBIT_DEMO:
        return {"testnet": False}
    elif BYBIT_TESTNET:
        return {"testnet": True}
    else:
        return {"testnet": False}

# WebSocket эндпоинты
if BYBIT_DEMO:
    BYBIT_WS_PUBLIC = "wss://stream.bybit.com/v5/public/spot"  # демо использует mainnet public
    BYBIT_WS_PRIVATE = "wss://stream-demo.bybit.com/v5/private"
elif BYBIT_TESTNET:
    BYBIT_WS_PUBLIC = "wss://stream-testnet.bybit.com/v5/public/spot"
    BYBIT_WS_PRIVATE = "wss://stream-testnet.bybit.com/v5/private"
else:
    BYBIT_WS_PUBLIC = "wss://stream.bybit.com/v5/public/spot"
    BYBIT_WS_PRIVATE = "wss://stream.bybit.com/v5/private"

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
TELEGRAM_ALLOWED_USER_IDS = [
    int(uid.strip()) for uid in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
]

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
