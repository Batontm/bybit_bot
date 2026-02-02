# SSH подключение к серверу Bybit Bot

## Параметры сервера
- **Host/IP:** `217.12.37.42`
- **Port:** `58291`
- **User:** `root`
- **SSH alias:** `bybit-bot`
- **Private key:** `~/.ssh/bybit_bot_ed25519`

## Быстрое подключение
```bash
ssh bybit-bot
```

## Явное подключение без alias
```bash
ssh -i ~/.ssh/bybit_bot_ed25519 -p 58291 root@217.12.37.42
```

## Конфиг `~/.ssh/config`
Убедись, что в файле `~/.ssh/config` на твоём Mac есть блок:

```sshconfig
Host bybit-bot
  HostName 217.12.37.42
  User root
  Port 58291
  IdentityFile ~/.ssh/bybit_bot_ed25519
  IdentitiesOnly yes
```

Права на файлы:
```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/config
chmod 600 ~/.ssh/bybit_bot_ed25519
```

## SSH Agent (чтобы не вводить passphrase каждый раз)
```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/bybit_bot_ed25519
```

## Проверки (для диагностики)
```bash
ssh -vvv bybit-bot
```

## Контекст для нового диалога (скопировать в чат)
```
Сервер: root@217.12.37.42:58291
SSH alias: bybit-bot
Ключ: ~/.ssh/bybit_bot_ed25519
Проект на сервере: /root/bybit_bot
Логи: /root/bybit_bot/logs/bot.log и /root/bybit_bot/logs/run.log
Запуск: /root/bybit_bot/run_bot.sh (venv -> /root/bybit_bot/.venv)
```
