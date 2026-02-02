import sys
import os
sys.path.insert(0, '/root/bybit_bot')

from bot.services.arbitrage_service import ArbitrageService
import logging

# Настройка логов для консоли
logging.basicConfig(level=logging.INFO)

def test():
    print("🚀 Тестирование динамического сканера арбитража...")
    service = ArbitrageService()
    
    pairs = service._get_liquid_pairs()
    print(f"\n✅ Список ликвидных пар ({len(pairs)}):")
    print(pairs[:10], "... and more")
    
    print("\n🔍 Сканирование funding rates для первых 5 пар...")
    service.ARBITRAGE_PAIRS = pairs[:5] # Временная подмена для быстрого теста
    opps = service.scan_funding_rates()
    
    for o in opps:
        print(f"   📌 {o['pair']}: Funding {o['funding_rate']*100:.4f}% | APY {o['apy']:.2f}%")

if __name__ == "__main__":
    test()
