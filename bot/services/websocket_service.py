"""
Сервис мониторинга цен через WebSocket
"""
import time
from typing import Dict, List, Optional
from pybit.unified_trading import WebSocket, HTTP
from config.settings import logger
from config.api_config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET

class PriceStreamService:
    """Сервис получения цен в реальном времени через WebSocket"""
    
    def __init__(self):
        self._ws: Optional[WebSocket] = None
        self._rest_client: Optional[HTTP] = None
        self._prices: Dict[str, float] = {}
        self._last_update: Dict[str, float] = {}
        self._subscribed_pairs: List[str] = []
        self._ws_connected = False
    
    @property
    def ws(self) -> Optional[WebSocket]:
        """Ленивая инициализация WebSocket"""
        if self._ws is None and not self._ws_connected:
            try:
                self._ws = WebSocket(
                    testnet=BYBIT_TESTNET,
                    channel_type="spot",
                    api_key=BYBIT_API_KEY,
                    api_secret=BYBIT_API_SECRET
                )
                self._ws_connected = True
                logger.info("✅ WebSocket подключён к Bybit")
            except Exception as e:
                logger.warning(f"⚠️ WebSocket недоступен: {e}. Используем REST fallback.")
                self._ws_connected = False
        return self._ws
    
    @property
    def rest_client(self) -> HTTP:
        """Ленивая инициализация REST клиента"""
        if self._rest_client is None:
            self._rest_client = HTTP(
                testnet=BYBIT_TESTNET,
                api_key=BYBIT_API_KEY,
                api_secret=BYBIT_API_SECRET
            )
        return self._rest_client
        
    def start(self):
        """Запустить WebSocket (pybit запускает поток внутри)"""
        # Инициализация происходит лениво при первом обращении
        _ = self.ws

    def subscribe(self, pairs: List[str]):
        """Подписаться на тикеры"""
        if not pairs:
            return
            
        # Определяем новые пары
        new_pairs = [p for p in pairs if p not in self._subscribed_pairs]
        if not new_pairs:
            return
            
        logger.info(f"🔌 WebSocket подписка на: {new_pairs}")
        
        # Подписка на тикеры
        # Формат аргументов для tickers: symbol=...
        # pybit требует список аргументов для подписки
        # Топик: tickers.{symbol}
        
        try:
            if self.ws is not None:
                self.ws.ticker_stream(
                    symbol=new_pairs,
                    callback=self._handle_ticker_message
                )
            
            self._subscribed_pairs.extend(new_pairs)
            
            # Предзагрузка цен через REST для быстрого старта
            self._preload_prices(new_pairs)
            
        except Exception as e:
            logger.error(f"❌ Ошибка подписки WebSocket: {e}")

    def _handle_ticker_message(self, message):
        """Обработчик сообщений от WS"""
        if 'data' not in message:
            return
            
        data = message['data']
        # data может быть словарем или списком? 
        # Обычно {'symbol': 'BTCUSDT', 'lastPrice': '...', ...}
        # Или {'topic': 'tickers.BTCUSDT', 'type': 'snapshot', 'data': {...}}
        
        try:
            symbol = data.get('symbol')
            last_price = data.get('lastPrice')
            
            if symbol and last_price:
                self._prices[symbol] = float(last_price)
                self._last_update[symbol] = time.time()
                # logger.debug(f"⚡ WS цена {symbol}: {last_price}")
        except Exception as e:
            logger.error(f"❌ Ошибка обработки WS сообщения: {e}")

    def get_price(self, pair: str) -> float:
        """
        Получить текущую цену.
        Использует кэш WS, если он свежий (< 1 мин). 
        Иначе Fallback на REST.
        """
        price = self._prices.get(pair)
        last_update = self._last_update.get(pair, 0)
        now = time.time()
        
        # Если цена есть и обновлялась недавно (менее 60 сек)
        if price and (now - last_update < 60):
            return price
            
        # Fallback
        logger.warning(f"⚠️ Устаревшая цена WS для {pair}, запрос REST...")
        return self._get_rest_price(pair)

    def _get_rest_price(self, pair: str) -> float:
        """Получить цену через REST"""
        try:
            response = self.rest_client.get_tickers(category="spot", symbol=pair)
            if response['retCode'] == 0:
                price = float(response['result']['list'][0]['lastPrice'])
                
                # Обновляем кэш
                self._prices[pair] = price
                self._last_update[pair] = time.time()
                return price
        except Exception as e:
            logger.error(f"❌ Ошибка REST запроса цены {pair}: {e}")
            
        return 0.0

    def _preload_prices(self, pairs: List[str]):
        """Предзагрузка цен"""
        try:
            for pair in pairs:
                self._get_rest_price(pair)
        except Exception as e:
            logger.error(f"⚠️ Ошибка предзагрузки цен: {e}")


# Глобальный экземпляр
websocket_service = PriceStreamService()
