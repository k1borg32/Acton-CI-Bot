"""
Minimal GitHub REST client for posting PR comments.

Uses a single Personal Access Token (PAT) — fine for one-team bots,
not multi-tenant. For a SaaS model the right path is to graduate to a
GitHub App with installation tokens.

If GITHUB_BOT_TOKEN is empty, post_pr_comment() is a no-op.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GitHubClient:
    """Bare-bones GitHub API client. Methods return None on any failure
    (4xx/5xx/network) and log — never raises into the caller, since posting
    a PR comment is a nice-to-have and must not fail a webhook run."""

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    async def post_pr_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> int | None:
        """POST /repos/{owner}/{repo}/issues/{pr}/comments. Returns the new
        comment id on success, None on any error."""
        if not self.enabled:
            return None
        url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "acton-ci-bot",
                    },
                    json={"body": body},
                )
                if resp.status_code in (200, 201):
                    return resp.json().get("id")
                logger.warning(
                    "GitHub comment failed on %s/%s#%d: %d %s",
                    owner, repo, pr_number, resp.status_code, resp.text[:200],
                )
                return None
        except httpx.HTTPError as e:
            logger.warning("GitHub comment HTTP error: %s", e)
            return None
