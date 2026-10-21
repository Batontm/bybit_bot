#!/bin/bash
# Установка / обновление бота на сервере Ubuntu (22.04+), запуск как systemd-служба.
#
# Первый раз (на сервере под root):
#   apt-get update && apt-get install -y git
#   git clone -b feature/entry-funnel https://github.com/Batontm/bybit_bot /root/bybit_bot
#   bash /root/bybit_bot/scripts/setup_server.sh
# Обновление кода потом — тот же скрипт: он сделает git pull и перезапустит службу.
set -e

APP_DIR=/root/bybit_bot
SERVICE=bybit-bot
cd "$APP_DIR"

echo "📦 Системные пакеты..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates curl >/dev/null

echo "🕒 Часовой пояс сервера: Europe/Kaliningrad"
timedatectl set-timezone Europe/Kaliningrad 2>/dev/null || true

if [ -d .git ]; then
    echo "🔄 Обновляю код из GitHub..."
    git pull --ff-only
fi
echo "   версия: $(git log --oneline -1)"

PY=python3
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "❌ Нужен Python 3.10+, на сервере: $($PY --version)"; exit 1
fi

if [ ! -d .venv ]; then
    echo "🐍 Создаю .venv..."
    $PY -m venv .venv
fi
echo "📚 Зависимости..."
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q
mkdir -p data logs

echo "🧪 Тесты..."
.venv/bin/python -m pytest -q || echo "⚠️ Есть упавшие тесты — посмотри вывод выше"

echo "⚙️ Служба systemd..."
cat > /etc/systemd/system/$SERVICE.service <<UNIT
[Unit]
Description=Bybit Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m bot.main
Restart=always
RestartSec=10
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:$APP_DIR/logs/run.log
StandardError=append:$APP_DIR/logs/run.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable $SERVICE >/dev/null 2>&1

# Ротация логов, чтобы не забить диск
cat > /etc/logrotate.d/$SERVICE <<ROT
$APP_DIR/logs/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
ROT

if [ ! -f .env ]; then
    echo ""
    echo "⏸️  Нет файла .env — служба установлена, но не запущена."
    echo "    Скопируй его с Mac:  scp ~/Documents/bybit_bot/.env root@<IP>:$APP_DIR/.env"
    echo "    и снова запусти этот скрипт."
    exit 0
fi
chmod 600 .env

echo ""
echo "🔑 Проверка ключей Bybit с этого сервера:"
.venv/bin/python scripts/check_api_keys.py || { echo "❌ Ключи не прошли проверку — служба не запущена"; exit 1; }

systemctl restart $SERVICE
sleep 15
echo ""
systemctl --no-pager --lines=0 status $SERVICE | head -5
echo ""
tail -n 25 logs/bot.log 2>/dev/null || tail -n 25 logs/run.log
echo ""
echo "✅ Готово. Лог: tail -f $APP_DIR/logs/bot.log | Стоп: systemctl stop $SERVICE | Старт: systemctl start $SERVICE"
