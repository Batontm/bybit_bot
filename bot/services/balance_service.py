"""
Сервис для работы с балансами и расчётом депозита
"""
from typing import Dict, Optional, List
from pybit.unified_trading import HTTP
from config.settings import logger
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, BYBIT_ACCOUNT_TYPE
from ..error_tracker import error_tracker


class BalanceService:
    """Управление балансами и депозитом"""
    
    def __init__(self):
        self.client = HTTP(
            testnet=BYBIT_TESTNET,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
            recv_window=60000
        )
    
    def get_wallet_balance(self) -> Optional[Dict]:
        """Получить баланс через wallet balance endpoint"""
        try:
            account_type = "UNIFIED" if str(BYBIT_ACCOUNT_TYPE).upper() == "UNIFIED" else "SPOT"
            response = self.client.get_wallet_balance(
                accountType=account_type
            )
            
            if response['retCode'] != 0:
                error_msg = f"Ошибка получения баланса: {response['retMsg']}"
                error_tracker.add_error("Bybit", "BalanceError", error_msg)
                return None
            
            account_data = response['result']['list'][0]
            coins = account_data.get('coin', [])
            
            balances = {}
            for coin_data in coins:
                coin = coin_data['coin']
                
                # ИСПРАВЛЕНО: безопасное преобразование с обработкой пустых строк
                wallet_balance_str = coin_data.get('walletBalance', '0')
                available_str = coin_data.get('availableToWithdraw', '0')
                
                # Если пустая строка, заменяем на '0'
                wallet_balance = float(wallet_balance_str if wallet_balance_str else '0')
                available = float(available_str if available_str else '0')
                
                if wallet_balance > 0:
                    balances[coin] = {
                        'coin': coin,
                        'total': wallet_balance,
                        'free': available,
                        'used': wallet_balance - available
                    }
            
            return balances
            
        except Exception as e:
            error_tracker.add_error("Bybit", "BalanceError", str(e))
            logger.error(f"❌ Ошибка получения баланса: {e}")
            return None
    
    def get_usdt_balance(self) -> Optional[Dict]:
        """Получить баланс USDT"""
        balances = self.get_wallet_balance()
        if balances and 'USDT' in balances:
            logger.debug(f"💰 USDT баланс: {balances['USDT']['total']:.2f}")
            return balances['USDT']
        else:
            logger.warning("⚠️ USDT не найден на балансе")
            return {'coin': 'USDT', 'free': 0.0, 'used': 0.0, 'total': 0.0}
    
    def get_all_balances(self) -> List[Dict]:
        """Получить все балансы с положительным значением"""
        try:
            balances_dict = self.get_wallet_balance()
            if balances_dict:
                return list(balances_dict.values())
            return []
            
        except Exception as e:
            error_tracker.add_error("Bybit", "BalanceError", str(e))
            logger.error(f"❌ Ошибка получения балансов: {e}")
            return []
    
    def get_deposit_usdt(self) -> float:
        """Получить общий депозит в USDT"""
        balance = self.get_usdt_balance()
        return balance['total'] if balance else 0.0
    
    def calculate_position_size(self, risk_percent: float, 
                               entry_price: float, 
                               sl_price: float) -> float:
        """Рассчитать размер позиции на основе риска"""
        deposit = self.get_deposit_usdt()
        
        if deposit <= 0:
            logger.error("❌ Депозит равен 0")
            return 0.0
        
        risk_amount = deposit * risk_percent
        sl_distance = abs(entry_price - sl_price)
        
        if sl_distance == 0:
            logger.error("❌ SL равен цене входа")
            return 0.0
        
        position_size_usdt = risk_amount / (sl_distance / entry_price)
        position_size = position_size_usdt / entry_price
        
        logger.debug(f"📊 Расчёт позиции: Депозит={deposit:.2f}, "
                    f"Риск={risk_amount:.2f}, Размер={position_size:.6f}")
        
        return position_size


# Глобальный экземпляр
balance_service = BalanceService()
