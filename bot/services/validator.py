"""
URL validation and repo metadata fetching.

Security-critical module: validates that user-provided URLs are safe
before any git operations occur.
"""

import re
from dataclasses import dataclass

import httpx

# Supported Git hosting platforms
# Each pattern captures (platform, owner, repo)
_PLATFORM_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "github",
        re.compile(
            r"^https://github\.com/([\w\-\.]+)/([\w\-\.]+?)(?:\.git)?/?$"
        ),
    ),
    (
        "gitlab",
        re.compile(
            r"^https://gitlab\.com/([\w\-\.]+(?:/[\w\-\.]+)*)/([\w\-\.]+?)(?:\.git)?/?$"
        ),
    ),
    (
        "bitbucket",
        re.compile(
            r"^https://bitbucket\.org/([\w\-\.]+)/([\w\-\.]+?)(?:\.git)?/?$"
        ),
    ),
]


@dataclass
class RepoInfo:
    """Validated repository information."""

    platform: str
    owner: str
    repo: str
    url: str
    size_kb: int | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


class ValidationError(Exception):
    """Raised when URL validation fails."""

    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        super().__init__(user_message)


def parse_repo_url(url: str) -> RepoInfo:
    """
    Parse and validate a repository URL.

    Raises ValidationError with a user-friendly message if the URL
    is not from a supported platform.
    """
    url = url.strip()

    for platform, pattern in _PLATFORM_PATTERNS:
        match = pattern.match(url)
        if match:
            groups = match.groups()
            owner, repo = groups[0], groups[1]
            return RepoInfo(
                platform=platform,
                owner=owner,
                repo=repo,
                url=url,
            )

    raise ValidationError(
        "❌ Неподдерживаемый URL.\n\n"
        "Поддерживаются:\n"
        "• `https://github.com/owner/repo`\n"
        "• `https://gitlab.com/owner/repo`\n"
        "• `https://bitbucket.org/owner/repo`"
    )


async def fetch_repo_size(info: RepoInfo) -> int | None:
    """
    Fetch the repository size in KB via the platform API.
    Returns None if the API is unreachable or the repo is private.
    """
    if info.platform != "github":
        # GitLab/Bitbucket API requires auth for size — skip for MVP
        return None

    api_url = f"https://api.github.com/repos/{info.owner}/{info.repo}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                api_url,
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                size_kb = data.get("size", 0)
                info.size_kb = size_kb
                return size_kb
    except (httpx.HTTPError, KeyError):
        pass

    return None


async def validate_repo(url: str, max_size_kb: int = 51200) -> RepoInfo:
    """
    Full validation pipeline:
    1. Parse URL format
    2. Check repo size (if available)

    Returns RepoInfo on success, raises ValidationError on failure.
    """
    info = parse_repo_url(url)

    size = await fetch_repo_size(info)
    if size is not None and size > max_size_kb:
        size_mb = size / 1024
        limit_mb = max_size_kb / 1024
        raise ValidationError(
            f"❌ Репозиторий слишком большой: {size_mb:.0f} MB "
            f"(лимит: {limit_mb:.0f} MB).\n\n"
            "Попробуйте репозиторий поменьше или обратитесь к администратору."
        )

    return info
