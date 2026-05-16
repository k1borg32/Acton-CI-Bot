# 🔬 Acton CI-Bot

Telegram-бот для автоматической проверки TON смарт-контрактов с помощью [Acton CLI](https://github.com/ton-blockchain/acton).

Отправьте ссылку на GitHub/GitLab/Bitbucket репозиторий → получите отчёт о сборке, тестах и линтинге прямо в Telegram.

## Quick Start

### 1. Создайте бота
Получите токен через [@BotFather](https://t.me/BotFather).

### 2. Настройте окружение
```bash
cp .env.example .env
# Отредактируйте .env — укажите BOT_TOKEN
```

### 3. Соберите runner-образ
Официального публичного Docker-образа `ghcr.io/ton-blockchain/acton` пока
**не существует**, поэтому собираем сами из релизного установщика:
```bash
docker compose --profile setup build acton-runner
# Проверка: должно вывести `acton 1.0.0 (...)`
docker run --rm acton-runner:latest --version
```

### 4. Запустите бота
> ⚠️ **Docker-compose-стек предполагает Linux-хост.**
> Он монтирует `/var/run/docker.sock` и хостовую папку `/tmp/acton-workspaces`
> для DooD-паттерна — на macOS/Windows Docker Desktop эти пути ведут в VM,
> а не в файловую систему хоста, и `-v $tmpdir:/workspace` в DooD-спавне
> не сработает. Для локальной разработки на Windows запускайте бота напрямую:
> `python -m bot.main` (docker CLI всё равно нужен, чтобы запускать runner).
```bash
docker compose up -d bot
```

### 5. Проверьте
Отправьте боту в Telegram:
```
/check https://github.com/example/ton-contract
```
Репозиторий должен быть **Acton-проектом** — с `Acton.toml` в корне.
Чистые FunC/Tolk-репо (например, `ton-blockchain/tolk-bench`) бот распознает
и ответит «это не Acton-проект» — это ожидаемое поведение, не баг.

## Архитектура

```
User → /check URL → Telegram Bot (aiogram 3 polling)
                         ↓
GitHub PR → /webhooks/github  (aiohttp, HMAC verify, fan-out by subscription)
                         ↓
                   URL Validator (whitelist + size check)
                         ↓
                   Job Queue (asyncio + rate limiting)
                         ↓
                   Docker Runner (sibling container, DooD)
                     acton build → test → check → fmt --check
                         ↓
                   Formatter → Telegram HTML report
```

## GitHub Webhooks (Phase 2)

The bot exposes `POST /webhooks/github` for automatic CI on pull requests.

**Setup per repo:**
1. In a Telegram chat (group or DM), admin runs:
   `/subscribe owner/repo`
2. In GitHub → repo Settings → Webhooks → **Add webhook**:
   - Payload URL: `https://your-bot-domain/webhooks/github`
   - Content type: `application/json`
   - Secret: same value as `GITHUB_WEBHOOK_SECRET` in your `.env`
   - Events: **Just the pull request** (or "Pull requests" only)
3. Open/sync/reopen a PR → the report appears in the subscribed chat.

Other commands:
- `/subscriptions` — list this chat's active subscriptions
- `/unsubscribe owner/repo` — remove

Only events with action `opened`, `synchronize`, or `reopened` trigger a run.
Other events return 202 ignored.

## Безопасность

- ✅ URL whitelist: только github.com, gitlab.com, bitbucket.org
- ✅ git clone: `--no-recurse-submodules`, `core.symlinks=false`, `core.hooksPath=/dev/null`
- ✅ Docker: `--network=none`, `--read-only`, `--memory`, `--cpus`, `--pids-limit`
- ✅ Rate limiting: per-user + global
- ✅ Non-root user в обоих контейнерах (bot + runner)

## Структура проекта

```
acton-ci-bot/
├── bot/
│   ├── main.py           # Entry point
│   ├── config.py         # Environment-based configuration
│   ├── handlers/
│   │   ├── check.py      # /check command
│   │   ├── common.py     # /start, /help
│   │   └── status.py     # /status
│   └── services/
│       ├── runner.py     # Docker runner (hardened)
│       ├── validator.py  # URL validation + repo size check
│       ├── formatter.py  # Telegram HTML report formatter
│       └── queue.py      # Async job queue + rate limiter
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

## Конфигурация

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `BOT_TOKEN` | — | Telegram Bot API token |
| `ADMIN_IDS` | — | Comma-separated admin Telegram IDs |
| `ACTON_DOCKER_IMAGE` | `acton-runner:latest` | Docker-образ Acton (собирается локально, см. шаг 3) |
| `CONTAINER_MEMORY` | `512m` | RAM-лимит контейнера |
| `CONTAINER_CPUS` | `1` | CPU-лимит |
| `MAX_CHECKS_PER_HOUR` | `5` | Лимит проверок/час на юзера |
| `MAX_CONCURRENT_GLOBAL` | `3` | Максимум параллельных прогонов |
| `MAX_REPO_SIZE_KB` | `51200` | Лимит размера репо (50MB) |

## License

MIT
