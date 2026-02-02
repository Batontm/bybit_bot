#!/bin/bash

echo "🛑 Остановка Bybit Trading Bot"

# Ищем процесс бота
PID=$(ps aux | grep "bot/main.py" | grep -v grep | awk '{print $2}')

if [ -z "$PID" ]; then
    echo "❌ Бот не запущен"
else
    echo "🔍 Найден процесс: $PID"
    kill -SIGTERM $PID
    echo "✅ Сигнал остановки отправлен"
    
    # Ждём завершения
    sleep 2
    
    # Проверяем, завершился ли процесс
    if ps -p $PID > /dev/null; then
        echo "⚠️ Процесс всё ещё работает, принудительная остановка"
        kill -9 $PID
    fi
    
    echo "✅ Бот остановлен"
fi