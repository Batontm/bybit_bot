import sys
import os
sys.path.insert(0, '/root/bybit_bot')

from bot.services.scanner_service import scanner_service
import logging

logging.basicConfig(level=logging.INFO)

def test():
    print("🚀 Тестирование сканера Top Gainers (Trade)...")
    
    # Тест 1: Новые дефолты (2M vol, 2% growth)
    print("\n📊 Сценарий 1: Проверка с новыми критериями (2M vol, 2% growth)")
    top = scanner_service.get_top_gainers(limit=5)
    print(f"✅ Найдено: {top}")
    
    # Тест 2: Очень агрессивный (500K vol) - для проверки Тестнета/пустого рынка
    print("\n📊 Сценарий 2: Агрессивный (500K vol, 1% growth)")
    top_agg = scanner_service.get_top_gainers(limit=10, min_volume_usdt=500_000)
    print(f"✅ Найдено: {top_agg}")

if __name__ == "__main__":
    test()
