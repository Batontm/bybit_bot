"""
Repository для работы с запросами к Perplexity
"""
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from config.settings import logger, TIMEZONE
from .connection import get_db, get_transaction


class LLMRequestsRepository:
    """Управление запросами к Perplexity API"""
    
    @staticmethod
    def create_request(request_data: Dict) -> int:
        """Записать запрос к LLM"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO llm_requests (
                    pair, timeframe, prompt_type, score, signal, summary,
                    rejection_reason, cost_usd, success, error_code, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                request_data['pair'],
                request_data['timeframe'],
                request_data.get('prompt_type', 'analysis'),
                request_data.get('score'),
                request_data.get('signal'),
                request_data.get('summary'),
                request_data.get('rejection_reason'),
                request_data.get('cost_usd', 0),
                request_data.get('success', True),
                request_data.get('error_code'),
                request_data.get('error_message'),
                request_data.get('created_at', datetime.now(TIMEZONE).isoformat())
            ))
            
            request_id = cursor.lastrowid
            
            if request_data.get('success'):
                logger.info(f"🤖 LLM запрос: {request_data['pair']} | "
                           f"Score: {request_data.get('score')} | "
                           f"Cost: ${request_data.get('cost_usd', 0):.4f}")
            else:
                logger.warning(f"⚠️ LLM ошибка: {request_data['pair']} | "
                              f"{request_data.get('error_code')}")
            
            return request_id
    
    @staticmethod
    def get_latest_analysis(pair: str, max_age_seconds: int = 300) -> Optional[Dict]:
        """Получить последний успешный анализ пары (если свежий)"""
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Вычисляем минимальное время
            min_time = (datetime.now(TIMEZONE) - timedelta(seconds=max_age_seconds)).isoformat()
            
            cursor.execute("""
                SELECT * FROM llm_requests
                WHERE pair = ? AND success = 1 AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (pair, min_time))
            
            row = cursor.fetchone()
            return dict(row) if row else None
    
    @staticmethod
    def get_daily_stats() -> Dict:
        """Получить статистику за сегодня"""
        with get_db() as conn:
            cursor = conn.cursor()
            
            today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_requests,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed,
                    SUM(cost_usd) as total_cost,
                    AVG(cost_usd) as avg_cost
                FROM llm_requests
                WHERE DATE(created_at) = ?
            """, (today,))
            
            row = cursor.fetchone()
            return dict(row) if row else {
                'total_requests': 0,
                'successful': 0,
                'failed': 0,
                'total_cost': 0.0,
                'avg_cost': 0.0
            }
    
    @staticmethod
    def get_period_stats(days: int = 7) -> Dict:
        """Получить статистику за период"""
        with get_db() as conn:
            cursor = conn.cursor()
            
            start_date = (datetime.now(TIMEZONE) - timedelta(days=days)).strftime("%Y-%m-%d")
            
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_requests,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed,
                    SUM(cost_usd) as total_cost,
                    AVG(cost_usd) as avg_cost
                FROM llm_requests
                WHERE DATE(created_at) >= ?
            """, (start_date,))
            
            row = cursor.fetchone()
            return dict(row) if row else {
                'total_requests': 0,
                'successful': 0,
                'failed': 0,
                'total_cost': 0.0,
                'avg_cost': 0.0
            }
    
    @staticmethod
    def get_monthly_cost() -> float:
        """Получить общую стоимость за текущий месяц"""
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Первый день текущего месяца
            first_day = datetime.now(TIMEZONE).replace(day=1).strftime("%Y-%m-%d")
            
            cursor.execute("""
                SELECT SUM(cost_usd) as monthly_cost
                FROM llm_requests
                WHERE DATE(created_at) >= ?
            """, (first_day,))
            
            row = cursor.fetchone()
            return row['monthly_cost'] or 0.0
    
    @staticmethod
    def get_rejection_logs(days: int = 7, limit: int = 50) -> List[Dict]:
        """
        Получить логи отказов для анализа "почему не вошли"
        
        Returns:
            List[Dict] с полями: pair, score, signal, summary, rejection_reason, created_at
        """
        with get_db() as conn:
            cursor = conn.cursor()
            
            start_date = (datetime.now(TIMEZONE) - timedelta(days=days)).isoformat()
            
            cursor.execute("""
                SELECT pair, score, signal, summary, rejection_reason, created_at
                FROM llm_requests
                WHERE success = 1 
                  AND rejection_reason IS NOT NULL 
                  AND rejection_reason != ''
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (start_date, limit))
            
            return [dict(row) for row in cursor.fetchall()]
    
    @staticmethod
    def update_rejection_reason(request_id: int, reason: str) -> bool:
        """Обновить причину отказа для существующего запроса"""
        with get_transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE llm_requests SET rejection_reason = ? WHERE id = ?
            """, (reason, request_id))
            return cursor.rowcount > 0
    
    @staticmethod
    def get_low_score_analyses(min_score: int = 40, max_score: int = 64, days: int = 7) -> List[Dict]:
        """
        Получить анализы с низким score для изучения причин
        """
        with get_db() as conn:
            cursor = conn.cursor()
            
            start_date = (datetime.now(TIMEZONE) - timedelta(days=days)).isoformat()
            
            cursor.execute("""
                SELECT pair, score, signal, summary, rejection_reason, created_at
                FROM llm_requests
                WHERE success = 1 
                  AND score BETWEEN ? AND ?
                  AND created_at >= ?
                ORDER BY created_at DESC
            """, (min_score, max_score, start_date))
            
            return [dict(row) for row in cursor.fetchall()]
