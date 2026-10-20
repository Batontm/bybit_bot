#!/bin/bash
# Запуск бота локально: ./run_bot.sh  (остановка — Ctrl+C или ./stop_bot.sh)
cd "$(dirname "$0")"

echo "🤖 Запуск Bybit Trading Bot"
echo "============================"

# Виртуальное окружение: .venv (setup_local.sh) или старое venv
if [ -d ".venv" ]; then
    source .venv/bin/activate
elif [ -d "venv" ]; then
    source venv/bin/activate
else
    echo "❌ Виртуальное окружение не найдено. Сначала запусти: ./setup_local.sh"
    exit 1
fi
echo "✅ Виртуальное окружение активировано"

if [ ! -f ".env" ]; then
    echo "❌ Файл .env не найден. Скопируй .env.example в .env и впиши ключи"
    exit 1
fi

# Python с python.org на macOS не видит системные сертификаты —
# берём набор certifi (иначе WebSocket/HTTPS падают с CERTIFICATE_VERIFY_FAILED)
CERT=$(python -c 'import certifi; print(certifi.where())' 2>/dev/null)
if [ -n "$CERT" ]; then
    export SSL_CERT_FILE="$CERT"
fi

mkdir -p data logs
echo ""
echo "🚀 Запуск бота... (лог: logs/bot.log)"
exec python -m bot.main
