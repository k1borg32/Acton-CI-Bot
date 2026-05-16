"""
GitHub webhook receiver.

Endpoints:
  POST /webhooks/github   — accept GitHub webhook events
  GET  /healthz           — liveness probe for Coolify/Traefik

For supported events (PR opened/synchronize/reopened) the handler:
  1. Verifies the X-Hub-Signature-256 HMAC against GITHUB_WEBHOOK_SECRET
  2. Acks GitHub immediately with 202 (webhook timeout is short)
  3. Looks up subscribed chats for the repo
  4. Runs the Acton pipeline once against the PR head
  5. Posts the report to every subscribed chat
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiohttp import web

from bot.config import AppConfig
from bot.services.formatter import format_report, format_webhook_header
from bot.services.queue import JobQueue
from bot.services.runner import run_acton_pipeline
from bot.services.subscriptions import SubscriptionStore
from bot.services.validator import RepoInfo

logger = logging.getLogger(__name__)

# GitHub PR actions that are worth running CI for. Skipping `closed`,
# `assigned`, `labeled` etc. — they don't change the code.
_HANDLED_PR_ACTIONS = {"opened", "synchronize", "reopened"}


@dataclass
class WebhookJob:
    """A pipeline job triggered by an incoming webhook."""
    repo: RepoInfo
    ref: str            # head SHA
    pr_number: int
    pr_title: str
    pr_author: str
    pr_url: str
    chat_ids: list[int]


def _verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time HMAC-SHA256 check against GitHub's signature header."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def _parse_pr_event(payload: dict[str, Any]) -> WebhookJob | None:
    """Extract a WebhookJob from a pull_request event payload, or None if
    the event/action isn't one we run CI for."""
    action = payload.get("action")
    if action not in _HANDLED_PR_ACTIONS:
        return None
    pr = payload.get("pull_request") or {}
    head = pr.get("head") or {}
    head_repo = head.get("repo") or {}
    base_repo = (payload.get("repository") or {})

    head_sha = head.get("sha")
    clone_url = head_repo.get("clone_url") or head_repo.get("html_url")
    owner_login = (head_repo.get("owner") or {}).get("login")
    repo_name = head_repo.get("name")
    if not (head_sha and clone_url and owner_login and repo_name):
        return None
    # Only GitHub for now — the URL validator pattern is the same anyway.
    if not clone_url.startswith("https://github.com/"):
        return None

    pr_user = (pr.get("user") or {}).get("login") or "unknown"
    pr_url = pr.get("html_url") or ""
    pr_number = pr.get("number") or 0
    pr_title = pr.get("title") or ""

    base_full = base_repo.get("full_name") or f"{owner_login}/{repo_name}"

    return WebhookJob(
        repo=RepoInfo(
            platform="github",
            owner=owner_login,
            repo=repo_name,
            url=clone_url.rstrip(".git"),  # validator's pattern doesn't want .git suffix
        ),
        ref=head_sha,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_author=pr_user,
        pr_url=pr_url,
        chat_ids=[],  # populated by handler after subscription lookup
    )

    _ = base_full  # currently unused but kept for future routing precision


async def _run_and_fan_out(
    job: WebhookJob,
    bot: Bot,
    config: AppConfig,
    queue: JobQueue,
) -> None:
    """Run the pipeline once, post the formatted report to each chat."""
    logger.info(
        "webhook job: %s PR#%d head=%s → %d chat(s)",
        job.repo.full_name, job.pr_number, job.ref[:7], len(job.chat_ids),
    )
    # Use a synthetic user id so per-user limits don't gate webhooks; the
    # global semaphore still applies.
    synthetic_user_id = -1 * abs(hash(job.repo.full_name)) % 10_000_000

    try:
        await queue.acquire(synthetic_user_id)
    except Exception as e:
        # Rate-limited at the global level — surface a tiny notice and bail
        logger.warning("webhook rate-limited: %s", e)
        for chat_id in job.chat_ids:
            try:
                await bot.send_message(
                    chat_id,
                    f"⏳ Очередь занята — PR #{job.pr_number} в "
                    f"<code>{job.repo.full_name}</code> подождёт.",
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("notify chat %s failed", chat_id)
        return

    try:
        result = await run_acton_pipeline(job.repo, config.runner, ref=job.ref)
        header = format_webhook_header(job)
        report = header + "\n" + format_report(result)
        for chat_id in job.chat_ids:
            try:
                await bot.send_message(chat_id, report, parse_mode="HTML")
            except Exception:
                logger.exception("post report to chat %s failed", chat_id)
    finally:
        queue.release(synthetic_user_id)


def make_webhook_app(
    *,
    bot: Bot,
    config: AppConfig,
    subscriptions: SubscriptionStore,
    queue: JobQueue,
    secret: str,
) -> web.Application:
    """Build the aiohttp app the bot serves alongside its Telegram polling."""

    routes = web.RouteTableDef()

    @routes.get("/healthz")
    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "subscriptions": subscriptions.count()})

    @routes.post("/webhooks/github")
    async def github_webhook(request: web.Request) -> web.Response:
        body = await request.read()

        if not secret:
            logger.error("webhook received but GITHUB_WEBHOOK_SECRET is not set")
            return web.json_response(
                {"ok": False, "error": "server misconfigured"}, status=500
            )

        sig = request.headers.get("X-Hub-Signature-256")
        if not _verify_signature(secret, body, sig):
            logger.warning("webhook with bad/missing signature from %s", request.remote)
            return web.json_response(
                {"ok": False, "error": "bad signature"}, status=401
            )

        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return web.json_response({"ok": True, "pong": True})

        if event != "pull_request":
            return web.json_response(
                {"ok": True, "ignored": f"event={event}"}, status=202
            )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.json_response(
                {"ok": False, "error": "invalid json"}, status=400
            )

        job = _parse_pr_event(payload)
        if job is None:
            return web.json_response(
                {"ok": True, "ignored": f"pr_action={payload.get('action')}"},
                status=202,
            )

        chat_ids = subscriptions.list_chats_for_repo(job.repo.full_name)
        if not chat_ids:
            return web.json_response(
                {"ok": True, "ignored": f"no_subscribers:{job.repo.full_name}"},
                status=202,
            )
        job.chat_ids = chat_ids

        # Fire-and-forget — must ACK GitHub within ~10s.
        asyncio.create_task(_run_and_fan_out(job, bot, config, queue))

        return web.json_response(
            {"ok": True, "scheduled": True, "subscribers": len(chat_ids)},
            status=202,
        )

    app = web.Application()
    app.add_routes(routes)
    return app
