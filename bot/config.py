"""
Configuration module — loads settings from environment variables.
"""

from dataclasses import dataclass, field
from os import environ


@dataclass(frozen=True)
class BotConfig:
    token: str = field(default_factory=lambda: environ["BOT_TOKEN"])
    admin_ids: list[int] = field(
        default_factory=lambda: [
            int(x.strip())
            for x in environ.get("ADMIN_IDS", "").split(",")
            if x.strip()
        ]
    )


@dataclass(frozen=True)
class RunnerConfig:
    docker_image: str = field(
        default_factory=lambda: environ.get(
            "ACTON_DOCKER_IMAGE", "acton-runner:latest"
        )
    )
    container_memory: str = field(
        default_factory=lambda: environ.get("CONTAINER_MEMORY", "512m")
    )
    container_cpus: str = field(
        default_factory=lambda: environ.get("CONTAINER_CPUS", "1")
    )
    container_pids_limit: str = field(
        default_factory=lambda: environ.get("CONTAINER_PIDS_LIMIT", "256")
    )
    clone_timeout: int = field(
        default_factory=lambda: int(environ.get("CLONE_TIMEOUT", "60"))
    )
    build_timeout: int = field(
        default_factory=lambda: int(environ.get("BUILD_TIMEOUT", "180"))
    )
    max_repo_size_kb: int = field(
        default_factory=lambda: int(environ.get("MAX_REPO_SIZE_KB", "51200"))
    )


@dataclass(frozen=True)
class RateLimitConfig:
    max_checks_per_hour: int = field(
        default_factory=lambda: int(environ.get("MAX_CHECKS_PER_HOUR", "5"))
    )
    max_concurrent_per_user: int = field(
        default_factory=lambda: int(environ.get("MAX_CONCURRENT_PER_USER", "1"))
    )
    max_concurrent_global: int = field(
        default_factory=lambda: int(environ.get("MAX_CONCURRENT_GLOBAL", "3"))
    )


@dataclass(frozen=True)
class WebhookConfig:
    secret: str = field(
        default_factory=lambda: environ.get("GITHUB_WEBHOOK_SECRET", "")
    )
    host: str = field(default_factory=lambda: environ.get("WEBHOOK_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(environ.get("WEBHOOK_PORT", "3000")))


@dataclass(frozen=True)
class AppConfig:
    bot: BotConfig = field(default_factory=BotConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    db_path: str = field(
        default_factory=lambda: environ.get("DB_PATH", "data/acton_bot.db")
    )
