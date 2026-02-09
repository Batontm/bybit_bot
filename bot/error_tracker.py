"""
Кольцевой буфер для отслеживания ошибок
"""
from collections import deque
from datetime import datetime
from typing import Dict, List
from config.settings import TIMEZONE, logger
from config.trading_config import ERROR_BUFFER_SIZE


class ErrorTracker:
    """Кольцевой буфер последних ошибок"""
    
    def __init__(self, max_size: int = ERROR_BUFFER_SIZE):
        self.errors = deque(maxlen=max_size)
        self.max_size = max_size
    
    def add_error(self, module: str, error_type: str, message: str, 
                  traceback: str = None) -> None:
        """Добавить ошибку в буфер"""
        error_entry = {
            'timestamp': datetime.now(TIMEZONE).isoformat(),
            'module': module,
            'error_type': error_type,
            'message': message,
            'traceback': traceback
        }
        
        self.errors.append(error_entry)
        logger.error(f"❌ [{module}] {error_type}: {message}")
    
    def get_recent_errors(self, count: int = 20, module: str = None) -> List[Dict]:
        """Получить последние N ошибок (опционально по модулю)"""
        errors_list = list(self.errors)
        
        if module:
            errors_list = [e for e in errors_list if e['module'] == module]
        
        return errors_list[-count:]
    
    def get_today_errors(self) -> List[Dict]:
        """Получить ошибки за сегодня"""
        today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        return [
            e for e in self.errors 
            if e['timestamp'].startswith(today)
        ]
    
    def clear(self) -> None:
        """Очистить буфер"""
        self.errors.clear()
        logger.info("🗑️ Буфер ошибок очищен")


# Глобальный экземпляр
error_tracker = ErrorTracker()
