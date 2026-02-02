# Деплой bybit_bot на сервер

## Параметры
- Сервер: `root@217.12.37.42:58291`
- Папка проекта на сервере: `/root/bybit_bot`
- Alias: `bybit-bot`

## Локально (Mac) — собрать архив
Из корня репозитория:

```bash
zip -r deploy.zip bot/ config/ -x "*.pyc" -x "*__pycache__*"
```

## Локально (Mac) — загрузить на сервер
```bash
scp -P 58291 deploy.zip root@217.12.37.42:/root/bybit_bot/
# или
scp deploy.zip bybit-bot:/root/bybit_bot/
```

## Сервер — обновить код и перезапустить
```bash
ssh bybit-bot
cd /root/bybit_bot

# распаковать
unzip -o deploy.zip

# остановить текущий процесс (если запущен)
pkill -f 'python.*bot/main.py' || true

# venv: в проекте используется .venv, но run_bot.sh ожидает venv
(test -d venv || (test -d .venv && ln -sf .venv venv))

# запуск
chmod +x ./run_bot.sh
mkdir -p logs
nohup ./run_bot.sh > logs/run.log 2>&1 &

# проверить
pgrep -af 'python.*bot/main.py'
 tail -n 120 logs/run.log
 tail -n 120 logs/bot.log
```

## Быстрые проверки после деплоя
```bash
cd /root/bybit_bot
pgrep -af 'python.*bot/main.py' || true

# последние ошибки
 tail -n 400 logs/bot.log | egrep -n 'ERROR|Traceback|Exception|retCode|HTTP 4|HTTP 5' | tail -n 120 || true

# место на диске
 df -h /
```

## Ротация логов (ручная, если log вырос)
Рекомендованный безопасный вариант:

```bash
cd /root/bybit_bot
ts=$(date +%Y%m%d_%H%M%S)

mv logs/bot.log logs/bot.log.$ts
: > logs/bot.log
gzip -f logs/bot.log.$ts

df -h /
ls -lah logs/
```

Примечание: если лог был очень большой, место на диске может освободиться не полностью, пока процесс не перезапущен.

## Частые проблемы

### 1) `telegram.error.Conflict: terminated by other getUpdates request`
Причина: запущено 2 экземпляра бота.

Решение:
```bash
pkill -f 'python.*bot/main.py' || true
nohup ./run_bot.sh > logs/run.log 2>&1 &
```

### 2) `HTTP 401` по Perplexity
Причина: нет `PERPLEXITY_API_KEY` в `/root/bybit_bot/.env`.

Решение:
```bash
cd /root/bybit_bot
nano .env
# добавить:
# PERPLEXITY_API_KEY=...

pkill -f 'python.*bot/main.py' || true
nohup ./run_bot.sh > logs/run.log 2>&1 &
```

### 3) `ErrCode: 170131 Insufficient balance` при закрытии
Обычно означает, что монета заблокирована открытыми ордерами (TP/SL). Исправлено в коде: при закрытии сначала отменяются ордера.
Если повторяется — нужно смотреть `orders` в БД и реальные открытые ордера на бирже.
