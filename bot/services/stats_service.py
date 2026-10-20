"""
Сервис статистики и отчётов
"""
from typing import Dict, List
from datetime import datetime, timedelta
from config.settings import TIMEZONE
from ..db.pnl_repo import PnLRepository
from ..db.trades_repo import TradesRepository


class StatsService:
    """Генерация отчётов и статистики"""
    
    def __init__(self):
        self.pnl_repo = PnLRepository()
        self.trades_repo = TradesRepository()
    
    def get_pnl_by_pairs_report(self, days: int = 7) -> str:
        """
        Отчёт PnL по парам за период
        """
        data = self.pnl_repo.get_pnl_by_pairs(days)
        
        if not data:
            return f"📊 PnL по монетам ({days} дней)\n\n❌ Нет данных"
        
        # Считаем итоги
        total_pnl = sum(item['total_pnl'] for item in data)
        total_trades = sum(item['total_trades'] for item in data)
        
        # Формируем отчёт
        lines = [f"📊 PnL по монетам (последние {days} дней)\n"]
        
        for item in data[:10]:  # Топ-10
            pair = item['pair']
            pnl = item['total_pnl']
            trades = item['total_trades']
            wins = item['total_wins']
            losses = item['total_losses']
            
            emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            win_rate = (wins / trades * 100) if trades > 0 else 0
            
            lines.append(
                f"{emoji} {pair}: {pnl:+.2f} USDT "
                f"({trades} сделок, WR: {win_rate:.1f}%)"
            )
        
        if len(data) > 10:
            lines.append(f"\n... ещё {len(data) - 10} пар")
        
        lines.append(f"\n📈 Всего: {total_pnl:+.2f} USDT")
        lines.append(f"📊 Сделок: {total_trades}")
        
        return "\n".join(lines)
    
    def get_pnl_by_days_report(self, days: int = 7) -> str:
        """
        Отчёт PnL по дням
        """
        data = self.pnl_repo.get_pnl_by_days(days)
        
        if not data:
            return f"📅 PnL по дням ({days} дней)\n\n❌ Нет данных"
        
        lines = [f"📅 PnL по дням (последние {days} дней)\n"]
        
        for item in data:
            date = item['date']
            pnl = item['daily_pnl']
            trades = item['daily_trades']
            
            emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            
            lines.append(f"{emoji} {date}: {pnl:+.2f} USDT ({trades} сделок)")
        
        return "\n".join(lines)
    
    def get_positions_status_report(self) -> str:
        """Отчёт по текущим позициям"""
        positions = self.trades_repo.get_open_positions()
        
        if not positions:
            return "📊 Статус позиций\n\n✅ Нет открытых позиций"
        
        lines = [f"📊 Открытых позиций: {len(positions)}\n"]
        
        total_unrealized = 0
        
        for pos in positions:
            pair = pos['pair']
            qty = pos['quantity']
            entry = pos['entry_price']
            current = pos.get('current_price', entry)
            tp = pos.get('tp_price')
            sl = pos.get('sl_price')
            pnl = pos.get('unrealized_pnl', 0)
            pnl_pct = pos.get('unrealized_pnl_percent', 0)
            
            total_unrealized += pnl
            
            emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            
            lines.append(f"{emoji} {pair}")
            lines.append(f"  Кол-во: {qty:.6f}")
            lines.append(f"  Вход: {entry:.2f}")
            lines.append(f"  Текущая: {current:.2f}")
            if tp:
                lines.append(f"  TP: {tp:.2f}")
            if sl:
                lines.append(f"  SL: {sl:.2f}")
            lines.append(f"  PnL: {pnl:+.2f} USDT ({pnl_pct:+.2f}%)")
            lines.append("")
        
        lines.append(f"💰 Общий нереализованный PnL: {total_unrealized:+.2f} USDT")
        
        return "\n".join(lines)


# Глобальный экземпляр
stats_service = StatsService()
