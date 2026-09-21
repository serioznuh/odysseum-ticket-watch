"""Read-only health evidence for the GitHub Actions half of the watcher."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import httpx

from . import detect

GITHUB_API_ROOT = "https://api.github.com"


class CloudStatusError(RuntimeError):
    """The public Actions API did not provide trustworthy liveness evidence."""


def latest_successful_scheduled_run(
    repository: str,
    workflow: str,
    *,
    timeout: float = 10.0,
) -> datetime | None:
    """Return when the newest successful scheduled workflow run completed.

    The repository is public, so this deliberately uses no credential.  Only
    scheduled successes count: a manual dispatch must not conceal a dead cron,
    and failed runs (including the workflow's Telegram credential probe) must
    not refresh liveness.
    """
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise CloudStatusError("cloud repository must be owner/name")
    if not workflow:
        raise CloudStatusError("cloud workflow must not be empty")

    owner, name = (quote(part, safe="") for part in parts)
    workflow_id = quote(workflow, safe="")
    url = (
        f"{GITHUB_API_ROOT}/repos/{owner}/{name}/actions/"
        f"workflows/{workflow_id}/runs"
    )
    try:
        response = httpx.get(
            url,
            params={"event": "schedule", "status": "success", "per_page": 1},
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "odysseum-ticket-watch",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CloudStatusError(f"GitHub Actions API unavailable: {exc}") from exc

    runs = body.get("workflow_runs") if isinstance(body, dict) else None
    if not isinstance(runs, list):
        raise CloudStatusError("GitHub Actions API returned an invalid run list")
    if not runs:
        return None

    completed = runs[0].get("updated_at") if isinstance(runs[0], dict) else None
    # GitHub uses RFC 3339's ``Z`` suffix; Python 3.9's fromisoformat (which
    # backs detect.parse_iso) accepts the equivalent explicit UTC offset only.
    if isinstance(completed, str) and completed.endswith("Z"):
        completed = f"{completed[:-1]}+00:00"
    parsed = detect.parse_iso(completed)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CloudStatusError("GitHub Actions API returned an invalid completion time")
    return detect.as_aware(parsed)
