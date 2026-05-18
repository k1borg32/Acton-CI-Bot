# Acton CI-Bot

A free, public Telegram bot that runs the official
[Acton CLI](https://github.com/ton-blockchain/acton) (TON smart-contract
toolkit for Tolk) against any public GitHub/GitLab/Bitbucket repo and
posts a CI report back to the chat.

Built on a single VPS by a single developer. Free to use; donations help.

Two ways to use it:

- **Ad-hoc** — paste a repo URL, get build/test/lint/format results in ~2 seconds.
- **Subscribed** — wire a GitHub webhook to the bot and get automatic checks on
  every pull request, with the report posted in the subscribed chat.

## What gets checked

For each repo (or PR head SHA), the bot spins up an ephemeral, hardened Docker
container that runs:

1. **🔨 Build** — `acton build`
2. **🧪 Tests** — `acton test`
3. **🔍 Lint** — `acton check`
4. **✨ Format** — `acton fmt --check`

If `acton build` fails, the remaining steps are skipped. If the repo isn't an
Acton project (no `Acton.toml`), the bot replies with a friendly "not an Acton
project" message instead of dumping the raw error.

## Talking to the public bot

Open Telegram and start a chat with the bot you've configured. Commands:

| Command | What it does |
|---|---|
| `/start` | Welcome + quick reference |
| `/help` | Full reference |
| `/check <url>` | Run CI on a public repo |
| `/status` | Queue depth |
| `/subscribe owner/repo` | (admin) auto-check the repo's PRs in this chat |
| `/unsubscribe owner/repo` | (admin) stop auto-checks |
| `/subscriptions` | List this chat's subscriptions |
| `/admin stats` | (admin) runtime stats + recent errors |

## Architecture

```
User → /check URL → Telegram Bot (aiogram 3 polling)
                       ↓
GitHub PR → POST /webhooks/github  (aiohttp + HMAC verify)
                       ↓
                 URL Validator (host whitelist + size check)
                       ↓
                 Job Queue (asyncio Lock, per-user + global limits)
                       ↓
                 Docker Runner (sibling container, DooD)
                   acton build → test → check → fmt --check
                       ↓
                 Formatter → Telegram HTML report
```

## Deploy your own

### Prerequisites

- A Linux host (VPS / bare metal) with **Docker installed**
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- (optional) A domain pointing at the host if you want webhooks

### One-time setup on the host

```bash
git clone https://github.com/k1borg32/Acton-CI-Bot.git
cd Acton-CI-Bot

# Build the Acton runner image. The bot does `docker run acton-runner:latest`
# for every step, so this image must exist on the host's docker daemon.
# Pinned to Acton v1.0.0 with an in-build smoke test.
docker build -t acton-runner:latest -f docker/Dockerfile.acton .
docker run --rm acton-runner:latest --version
# → acton 1.0.0 (...)

# Configure the bot
cp .env.example .env
# Edit .env:
#   BOT_TOKEN=<from @BotFather>
#   ADMIN_IDS=<your Telegram user id>
#   GITHUB_WEBHOOK_SECRET=<openssl rand -hex 32>   # only if using webhooks
#   DOCKER_GID=<getent group docker | cut -d: -f3> # almost always 999, but verify
```

### Run via docker-compose (Linux)

```bash
docker compose up -d --build bot
docker compose logs -f bot
```

The compose stack mounts `/var/run/docker.sock` (so the bot can spawn runner
containers) and `/tmp` (so per-job tempdirs are visible to the host daemon
for bind-mounts). The bot listens on `:3000` for webhooks and `/healthz`.

### Run via Coolify (or any other PaaS that takes a Dockerfile)

Use the project's `Dockerfile` directly. Required configuration in your PaaS:

- **Build arg** `DOCKER_GID` = the host's docker group GID (`getent group docker | cut -d: -f3`)
- **Bind mount** host `/var/run/docker.sock` → container `/var/run/docker.sock`
- **Bind mount** host `/tmp` → container `/tmp`
- **Persistent storage** for `/app/data` (SQLite subscription DB)
- **Env vars**: `BOT_TOKEN`, `ADMIN_IDS`, `GITHUB_WEBHOOK_SECRET` (if using webhooks)
- **Healthcheck**: `GET /healthz` on port 3000 (image ships `curl`, so the default
  Coolify healthcheck works out of the box)
- **Domain**: if you want webhooks, expose port 3000 over HTTPS. GitHub will
  refuse webhooks served from an untrusted cert.

The image's entrypoint auto-chowns `/app/data` on startup, so fresh bind mounts
that start out root-owned are repaired automatically.

You also need to build `acton-runner:latest` on the same host's docker daemon
(Coolify only builds the bot image). See "One-time setup on the host" above —
SSH to the host once and run the `docker build` line.

### Use it

```bash
# Manual check (any Telegram chat with the bot)
/check https://github.com/owner/repo

# Auto-check every PR in a repo
/subscribe owner/repo                       # in the chat that should receive reports
# then in the repo: Settings → Webhooks → Add webhook
#   Payload URL:   https://<your-domain>/webhooks/github
#   Content type:  application/json
#   Secret:        <same as GITHUB_WEBHOOK_SECRET>
#   Events:        Just the pull request
```

## Sandboxing

Every step runs in a fresh container with:

- `--network=none` — no outbound network
- `--cap-drop=ALL` — no Linux capabilities
- `--security-opt=no-new-privileges` — blocks setuid escalation
- `--pids-limit=256`, `--memory=512m`, `--cpus=1` — resource caps
- `--rm` + ephemeral tempdir per job — no state carries between runs
- Non-root user (`runner`, UID 1000) inside the container

The bot itself runs as `botuser` (UID 1000), only the entrypoint runs as root
to fix volume ownership before `runuser`-dropping privileges.

Clones use `--no-recurse-submodules`, `core.symlinks=false`,
`core.hooksPath=/dev/null`, `protocol.file.allow=never`, and
`core.autocrlf=false` to neutralize known supply-chain vectors.

URLs are whitelisted to `github.com`, `gitlab.com`, `bitbucket.org` only.

## Configuration reference

All bot configuration is via environment variables (or `.env`).

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | *(required)* | Telegram bot token from @BotFather |
| `ADMIN_IDS` | `""` | Comma-separated Telegram user IDs allowed to manage subscriptions / run `/admin` |
| `ACTON_DOCKER_IMAGE` | `acton-runner:latest` | The image the bot spawns for each step |
| `CONTAINER_MEMORY` | `512m` | Runner RAM cap |
| `CONTAINER_CPUS` | `1` | Runner CPU cap |
| `CONTAINER_PIDS_LIMIT` | `256` | Runner pid cap |
| `CLONE_TIMEOUT` | `60` | `git clone` timeout (seconds) |
| `BUILD_TIMEOUT` | `180` | Per-step timeout (seconds) |
| `MAX_REPO_SIZE_KB` | `51200` | Repo-size limit (50 MB) |
| `MAX_CHECKS_PER_HOUR` | `5` | Per-user `/check` quota (60 min window) |
| `MAX_CHECKS_PER_DAY` | `30` | Per-user `/check` quota (24 h window) |
| `MAX_CONCURRENT_PER_USER` | `1` | Per-user in-flight cap |
| `MAX_CONCURRENT_GLOBAL` | `3` | Global in-flight cap |
| `MAX_CHECKS_GLOBAL_PER_DAY` | `1000` | Capacity guard for the host (24 h window) |
| `DONATE_TON_ADDRESS` | `""` | If set, enables `/donate` + main-menu button |
| `DB_PATH` | `data/acton_bot.db` | SQLite subscription DB |
| `GITHUB_WEBHOOK_SECRET` | `""` | HMAC secret for `POST /webhooks/github`. Empty = endpoint always 500s |
| `WEBHOOK_HOST` | `0.0.0.0` | HTTP bind host |
| `WEBHOOK_PORT` | `3000` | HTTP bind port |
| `DOCKER_GID` | `999` | Host's docker group GID — passed as a build arg into the bot image |

## Running the test suite

```bash
BOT_TOKEN=dummy ACTON_DOCKER_IMAGE=acton-runner:latest python -m scripts.e2e_test
```

24+ tests covering validator, queue, formatter, subscriptions, webhook signature/
event parsing, plus runner integration against `tolk-bench` (non-Acton repo →
friendly message) and a freshly scaffolded counter template (all 4 steps green).

## License

MIT
