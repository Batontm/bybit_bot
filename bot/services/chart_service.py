"""
Сервис для генерации графиков
"""
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import io
import os
from typing import List, Dict, Optional
from datetime import datetime
from config.settings import logger

# Установка бэкенда для Matplotlib (чтобы не требовал GUI)
plt.switch_backend('Agg')

class ChartService:
    """Сервис генерации графиков"""
    
    def generate_chart(self, pair: str, klines: List[Dict], 
                      analysis: Optional[Dict] = None) -> Optional[bytes]:
        """
        Создать изображение графика с свечами и целями
        
        Args:
            pair: Торговая пара
            klines: Список свечей (OHLCV)
            analysis: Результат анализа (для TP/SL)
            
        Returns:
            bytes: Изображение в формате PNG
        """
        try:
            if not klines:
                return None
            
            # Подготовка данных
            df = pd.DataFrame(klines)
            df['startTime'] = pd.to_numeric(df['startTime'])
            df['open'] = pd.to_numeric(df['open'])
            df['high'] = pd.to_numeric(df['high'])
            df['low'] = pd.to_numeric(df['low'])
            df['close'] = pd.to_numeric(df['close'])
            
            # Конвертация времени
            df['date'] = pd.to_datetime(df['startTime'], unit='ms')
            
            # Создание графика
            fig, ax = plt.subplots(figsize=(10, 6))
            
            # Рисуем свечи (упрощённо, через линии)
            # Зеленые свечи (рост)
            up = df[df.close >= df.open]
            down = df[df.close < df.open]
            
            # Фитили
            ax.vlines(df.date, df.low, df.high, color='gray', linewidth=1, alpha=0.5)
            
            # Тела свечей
            width = 0.03 # Ширина свечи (нужно подбирать в зависимости от таймфрейма)
            # Для точного отображения ширины лучше использовать ширину в днях, но для простоты используем линии
            
            # Используем bar chart для тел свечей
            # Ширина бара в днях. 1 час = 1/24 дня.
            # width = 0.03 ~ 43 минуты
            
            ax.bar(up.date, up.close - up.open, width=width, bottom=up.open, color='green', alpha=0.8)
            ax.bar(down.date, down.close - down.open, width=width, bottom=down.open, color='red', alpha=0.8)
            
            # Настройка осей
            ax.xaxis_date()
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            plt.xticks(rotation=45)
            
            current_price = df.iloc[-1]['close']
            
            # Добавляем уровни из анализа
            if analysis:
                target = analysis.get('target', 0)
                stop_loss = analysis.get('stop_loss', 0)
                signal = analysis.get('signal', 'WAIT')
                
                # Текущая цена
                ax.axhline(y=current_price, color='blue', linestyle='--', alpha=0.5, label=f'Price: {current_price}')
                
                # Цель (TP)
                if target > 0:
                    color = 'green' if target > current_price else 'red'
                    ax.axhline(y=target, color=color, linestyle='--', linewidth=1.5, label=f'Target: {target}')
                    
                # Стоп-лосс (SL)
                if stop_loss > 0:
                    ax.axhline(y=stop_loss, color='orange', linestyle=':', linewidth=1.5, label=f'Stop Loss: {stop_loss}')
                
                # Стрелка направления
                if signal == 'BUY':
                    ax.annotate('BUY', xy=(df.iloc[-1]['date'], df.iloc[-1]['low']), 
                                xytext=(df.iloc[-1]['date'], df.iloc[-1]['low']*0.98),
                                arrowprops=dict(facecolor='green', shrink=0.05))
                elif signal == 'AVOID' or signal == 'SELL':
                    ax.annotate('SELL/AVOID', xy=(df.iloc[-1]['date'], df.iloc[-1]['high']), 
                                xytext=(df.iloc[-1]['date'], df.iloc[-1]['high']*1.02),
                                arrowprops=dict(facecolor='red', shrink=0.05))

            plt.title(f"{pair} Price Chart")
            plt.xlabel("Time (UTC)")
            plt.ylabel("Price (USDT)")
            plt.grid(True, alpha=0.3)
            plt.legend(loc='best')
            
            # Сохранение в буфер
            buf = io.BytesIO()
            plt.savefig(buf, format='png', bbox_inches='tight')
            buf.seek(0)
            plt.close(fig)
            
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"❌ Ошибка генерации графика: {e}")
            return None


# Глобальный экземпляр
chart_service = ChartService()
