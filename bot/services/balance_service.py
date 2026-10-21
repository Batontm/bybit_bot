"""
Сервис для работы с балансами и расчётом депозита
"""
from typing import Dict, Optional, List
from pybit.unified_trading import HTTP
from config.settings import logger
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, BYBIT_ACCOUNT_TYPE, get_pybit_kwargs
from ..error_tracker import error_tracker


DUST_THRESHOLD_USD = 5.0  # Балансы дешевле $5 считаются пылью


def _num(v) -> float:
    """Число из ответа Bybit: пустые строки и None → 0."""
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


class BalanceService:
    """Управление балансами и депозитом"""
    
    def __init__(self):
        self.client = HTTP(
            **get_pybit_kwargs(),
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
            recv_window=60000
        )
        self.last_total_equity: Optional[float] = None  # totalEquity аккаунта в USD
    
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
            self.last_total_equity = _num(account_data.get('totalEquity'))
            
            balances = {}
            for coin_data in coins:
                coin = coin_data['coin']
                wallet_balance = _num(coin_data.get('walletBalance'))
                # availableToWithdraw устарел с 09.01.2025 и приходит пустым —
                # свободно = баланс − заблокировано ордерами (спот) − маржа ордеров
                locked = _num(coin_data.get('locked')) + _num(coin_data.get('totalOrderIM'))
                available = max(0.0, wallet_balance - locked)
                usd_value = _num(coin_data.get('usdValue'))
                
                if wallet_balance > 0:
                    balances[coin] = {
                        'coin': coin,
                        'total': wallet_balance,
                        'free': available,
                        'used': wallet_balance - available,
                        'usd_value': usd_value,
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
        """
        Капитал для расчёта риска: USDT на счёте, но не больше TRADING_CAPITAL_LIMIT_USDT
        (если задан) — так на реальном счёте можно отдать боту только часть денег.
        """
        from config.trading_config import TRADING_CAPITAL_LIMIT_USDT
        balance = self.get_usdt_balance()
        total = balance['total'] if balance else 0.0
        if TRADING_CAPITAL_LIMIT_USDT and TRADING_CAPITAL_LIMIT_USDT > 0:
            return min(total, float(TRADING_CAPITAL_LIMIT_USDT))
        return total
    
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
