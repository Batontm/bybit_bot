"""Pytest config: корень проекта в sys.path и отдельная БД для тестов."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Тесты пишут позиции/ордера в SQLite — никогда не в рабочую data/bot.db
os.environ.setdefault("BOT_DB_PATH", str(Path(tempfile.mkdtemp(prefix="bybit_bot_tests_")) / "test.db"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
