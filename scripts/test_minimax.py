#!/usr/bin/env python3
"""
Тест Minimax AI API
Base URL: https://api.minimax.io/v1
"""
from openai import OpenAI

MINIMAX_API_KEY = "sk-api-ccSKo6gbSE7sHYWj5ZqLEuQfcJQ0gDrHqE_DXKyLIsirkPIlM7tT-v0r6ohZKssvdZbkqGVe4e-HFH5RCr9lIXzYbHU2zIxqTUBxGkSMt4ALLHWxqrSScbI"
MINIMAX_BASE_URL = "https://api.minimax.io/v1"

client = OpenAI(
    api_key=MINIMAX_API_KEY,
    base_url=MINIMAX_BASE_URL
)

print("🧪 Тестирование Minimax AI API...")
print(f"Base URL: {MINIMAX_BASE_URL}")

try:
    response = client.chat.completions.create(
        model="MiniMax-Text-01",  # или MiniMax-M1, MiniMax-M2.1
        messages=[
            {"role": "system", "content": "Ты финансовый аналитик криптовалют."},
            {"role": "user", "content": "Скажи одним предложением: что такое BTC?"}
        ],
        max_tokens=100
    )
    
    print("\n✅ Успешно!")
    print(f"Модель: {response.model}")
    print(f"Ответ: {response.choices[0].message.content}")
    print(f"Tokens: {response.usage}")
    
except Exception as e:
    print(f"\n❌ Ошибка: {e}")
