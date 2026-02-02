#!/bin/bash
# ============================================================
# DEPLOY SCRIPT для Bybit Bot
# ============================================================

# 1. Остановить старый бот
echo "🛑 Останавливаем старый бот..."
systemctl stop bybit_bot

# 2. Бэкап старых данных (на всякий случай)
if [ -d "/root/bybit_bot/data" ]; then
    cp -r /root/bybit_bot/data /root/bybit_bot_backup_data_$(date +%Y%m%d_%H%M%S)
    echo "💾 Бэкап данных создан"
fi

# 3. Удалить старые файлы (кроме .env и data)
cd /root/bybit_bot
find . -maxdepth 1 ! -name '.env' ! -name 'data' ! -name '.' -exec rm -rf {} +
echo "🗑️ Старые файлы удалены"

# 4. Распаковать новую версию
cd /root
unzip -o deploy.zip -d /root/bybit_bot
echo "📦 Новая версия распакована"

# 5. Установить зависимости
cd /root/bybit_bot
source .venv/bin/activate
pip install --upgrade pip
pip install pybit python-telegram-bot python-dotenv httpx apscheduler pytz numpy
echo "📚 Зависимости установлены"

# 6. Перезапустить бот
systemctl restart bybit_bot
echo "✅ Бот перезапущен!"

# 7. Показать логи
echo "📋 Логи (Ctrl+C для выхода):"
journalctl -u bybit_bot -f
