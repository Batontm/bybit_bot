#!/bin/bash
# Установка бота для локального запуска (macOS / Linux).
# Создаёт виртуальное окружение .venv и ставит зависимости из requirements.txt.
#   ./setup_local.sh
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "❌ Нужен Python 3.10 или новее, сейчас: $("$PY" --version 2>&1)"
    echo "   Установи свежий Python с https://www.python.org/downloads/ и запусти снова."
    exit 1
fi
echo "🐍 $("$PY" --version)"

if [ ! -d .venv ]; then
    echo "📦 Создаю виртуальное окружение .venv..."
    "$PY" -m venv .venv
fi
source .venv/bin/activate

echo "📚 Ставлю зависимости..."
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt -q
echo "✅ Зависимости установлены"

if [ ! -f .env ]; then
    cp .env.example .env
    echo "📝 Создан .env из шаблона — впиши ключи Bybit"
fi

mkdir -p data logs

echo ""
echo "🔑 Проверка ключей Bybit:"
python scripts/check_api_keys.py || true

echo ""
echo "🧪 Тесты:"
python -m pytest -q || true

echo ""
echo "Готово. Запуск бота:  ./run_bot.sh     Остановка:  ./stop_bot.sh  (или Ctrl+C)"
