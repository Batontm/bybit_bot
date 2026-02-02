#!/usr/bin/env python3
"""
Скрипт для продажи всех монет (BTC, ETH, SOL) на бирже Bybit.
Конвертирует всё в USDT.

Запуск: python scripts/sell_all.py
"""
import sys
sys.path.insert(0, '/root/bybit_bot')

from pybit.unified_trading import HTTP
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET

# Точность qty для каждой пары
QTY_PRECISION = {
    'BTCUSDT': 6,
    'ETHUSDT': 4,
    'SOLUSDT': 2,
}

def format_qty(pair: str, quantity: float) -> str:
    """Форматировать quantity для Bybit API"""
    precision = QTY_PRECISION.get(pair, 2)
    rounded = round(quantity, precision)
    if precision == 0:
        return str(int(rounded))
    formatted = f"{rounded:.{precision}f}".rstrip('0').rstrip('.')
    return formatted if formatted else "0"


def get_balances(client) -> dict:
    """Получить балансы всех монет"""
    wallet = client.get_wallet_balance(accountType="UNIFIED")
    
    if wallet['retCode'] != 0:
        print(f"❌ Ошибка получения балансов: {wallet['retMsg']}")
        return {}
    
    balances = {}
    coins = wallet['result']['list'][0].get('coin', [])
    
    for coin in coins:
        symbol = coin['coin']
        balance = float(coin.get('walletBalance', 0))
        if balance > 0:
            balances[symbol] = balance
    
    return balances


def sell_coin(client, coin: str, amount: float) -> bool:
    """Продать монету за USDT"""
    pair = f"{coin}USDT"
    formatted_qty = format_qty(pair, amount)
    
    print(f"\n📤 Продаём {formatted_qty} {coin}...")
    
    try:
        response = client.place_order(
            category="spot",
            symbol=pair,
            side="Sell",
            orderType="Market",
            qty=formatted_qty,
            marketUnit="baseCoin"
        )
        
        if response['retCode'] == 0:
            order_id = response['result']['orderId']
            print(f"   ✅ Ордер {order_id} размещён!")
            return True
        else:
            print(f"   ❌ Ошибка: {response['retMsg']}")
            return False
            
    except Exception as e:
        print(f"   ❌ Исключение: {e}")
        return False


def main():
    print("=" * 60)
    print("🔄 ПРОДАЖА ВСЕХ МОНЕТ")
    print("=" * 60)
    
    # Подключение к Bybit
    client = HTTP(
        testnet=True,
        api_key=BYBIT_API_KEY,
        api_secret=BYBIT_API_SECRET,
        recv_window=20000
    )
    
    # Получаем балансы
    print("\n📊 Текущие балансы:")
    balances = get_balances(client)
    
    for coin, amount in balances.items():
        print(f"   {coin}: {amount}")
    
    # Монеты для продажи (кроме USDT)
    coins_to_sell = ['BTC', 'ETH', 'SOL']
    
    sold = []
    failed = []
    
    for coin in coins_to_sell:
        if coin in balances and balances[coin] > 0:
            # Минимальная сумма для продажи (примерно $5)
            min_values = {'BTC': 0.00005, 'ETH': 0.001, 'SOL': 0.05}
            min_qty = min_values.get(coin, 0.001)
            
            if balances[coin] >= min_qty:
                success = sell_coin(client, coin, balances[coin])
                if success:
                    sold.append(coin)
                else:
                    failed.append(coin)
            else:
                print(f"\n⚠️ {coin}: слишком маленький баланс ({balances[coin]})")
    
    # Результаты
    print("\n" + "=" * 60)
    print("📋 РЕЗУЛЬТАТЫ:")
    print("=" * 60)
    
    if sold:
        print(f"✅ Продано: {', '.join(sold)}")
    if failed:
        print(f"❌ Не удалось: {', '.join(failed)}")
    
    # Финальные балансы
    print("\n📊 Балансы после продажи:")
    new_balances = get_balances(client)
    for coin, amount in new_balances.items():
        print(f"   {coin}: {amount}")
    
    print("\n✅ Готово!")


if __name__ == "__main__":
    main()
