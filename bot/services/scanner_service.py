"""
Сервис для сканирования рынка и поиска перспективных монет
"""
from typing import List, Optional
from pybit.unified_trading import HTTP
from config.settings import logger
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET, get_pybit_kwargs


class MarketScanner:
    """Сканер рынка для поиска Top Gainers"""
    
    # Исключаем стейблы и левередж токены
    EXCLUDED_TOKENS = [
        'USDC', 'DAI', 'FDUSD', 'USDE', 'EUR', 'BUSD', 'USDD', 'TUSD', 'PYUSD',
        'USDV', 'GUSD', 'ZUSD'
    ]
    
    LEVERAGE_SUFFIXES = ['3L', '3S', '2L', '2S', '5L', '5S']
    
    def __init__(self):
        # Для сканирования публичных данных тикеров ключи не нужны.
        # Это предотвращает ошибки аутентификации при использовании Testnet ключей на Mainnet URL.
        self.client = HTTP(testnet=False)
    
    def get_top_gainers(self, limit: int = 5, min_volume_usdt: float = 1_000_000) -> List[str]:
        """
        Найти топ монет по росту за 24ч
        
        Criteria:
        1. Pair is USDT
        2. Not excluded (stables, leverage)
        3. Volume > min_volume_usdt
        4. 1.5% <= Change% <= 100%
        5. EXISTS in current environment (optional verification)
        """
        try:
            from config.api_config import BYBIT_TESTNET, BYBIT_DEMO
            
            # Получаем все тикеры (с Mainnet для поиска хайповых монет)
            response = self.client.get_tickers(category="spot")
            
            if response['retCode'] != 0:
                logger.error(f"❌ Ошибка получения тикеров: {response['retMsg']}")
                return []
            
            # Если мы на тестнете/демо, нам нужно знать, какие монеты там вообще есть
            supported_symbols = set()
            if BYBIT_TESTNET or BYBIT_DEMO:
                try:
                    env_client = HTTP(**get_pybit_kwargs())
                    instr_resp = env_client.get_instruments_info(category="spot")
                    if instr_resp['retCode'] == 0:
                        supported_symbols = {item['symbol'] for item in instr_resp['result']['list']}
                        logger.debug(f"🔍 Тестнет поддерживает {len(supported_symbols)} монет")
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось получить список монет тестнета: {e}")
            
            tickers = response['result']['list']
            candidates = []
            
            count_skipped_vol = 0
            count_skipped_change = 0
            
            for ticker in tickers:
                symbol = ticker['symbol']
                
                # 1. Только USDT пары
                if not symbol.endswith('USDT'):
                    continue
                
                base_coin = symbol.replace('USDT', '')
                
                # 2. Фильтр стейблкоинов (точное совпадение базовой монеты)
                if base_coin in self.EXCLUDED_TOKENS:
                    continue
                
                # 2.1 Фильтр по доступности на Тестнете (если применимо)
                if (BYBIT_TESTNET or BYBIT_DEMO) and supported_symbols and symbol not in supported_symbols:
                    continue
                    
                # 3. Фильтр токенов с плечом (суффиксы)
                if any(symbol.startswith(token) for token in self.LEVERAGE_SUFFIXES) or \
                   any(symbol.endswith(suffix + 'USDT') for suffix in self.LEVERAGE_SUFFIXES):
                    continue
                
                try:
                    price_change_percent = float(ticker['price24hPcnt']) * 100
                    volume_24h = float(ticker['turnover24h'])

                    if price_change_percent > 10.0:
                        continue
                    
                    # 3. Фильтр по объему
                    if volume_24h < min_volume_usdt:
                        count_skipped_vol += 1
                        continue
                    
                    # 4. Фильтр по росту (1.5-100%)
                    if 1.5 <= price_change_percent <= 100.0:
                        candidates.append({
                            'symbol': symbol,
                            'change': price_change_percent,
                            'volume': volume_24h
                        })
                    else:
                        count_skipped_change += 1
                        
                except (ValueError, TypeError):
                    continue
            
            # 5. Сортировка по росту (desc)
            candidates.sort(key=lambda x: x['change'], reverse=True)
            
            # Топ N
            top_gainers = [c['symbol'] for c in candidates[:limit]]
            
            if top_gainers:
                logger.info(f"🚀 Сканер: топ-{len(top_gainers)}: {', '.join(top_gainers)}")
                logger.debug(f"ℹ️ Проверено {len(tickers)} монет. Пропущено (объем): {count_skipped_vol}, Пропущено (рост): {count_skipped_change}")
            else:
                logger.info(f"ℹ️ Сканер: подходящих монет не найдено (проверено {len(tickers)})")
                
            return top_gainers
            
        except Exception as e:
            logger.error(f"❌ Ошибка сканера: {e}")
            return []


# Глобальный экземпляр
scanner_service = MarketScanner()
