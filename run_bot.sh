#!/bin/bash

echo "🤖 Запуск Bybit Trading Bot"
echo "============================"

# Активируем виртуальное окружение
if [ -d "venv" ]; then
    source venv/bin/activate
    echo "✅ Виртуальное окружение активировано"
else
    echo "❌ Виртуальное окружение не найдено"
    echo "Запустите сначала: python -m venv venv"
    exit 1
fi

# Проверяем наличие .env
if [ ! -f ".env" ]; then
    echo "❌ Файл .env не найден"
    echo "Скопируйте .env.example в .env и заполните ключи"
    exit 1
fi

# Запускаем бота
echo ""
echo "🚀 Запуск бота..."
python bot/main.py