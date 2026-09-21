"""Read-only health evidence for the GitHub Actions half of the watcher."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from . import detect

GITHUB_API_ROOT = "https://api.github.com"
SCHEDULE_INTERVAL = timedelta(minutes=15)
MAX_RUNS_PER_PAGE = 100


class CloudStatusError(RuntimeError):
    """The public Actions API did not provide trustworthy liveness evidence."""


def _github_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise CloudStatusError(f"GitHub Actions API returned an invalid {field}")
    # GitHub uses RFC 3339's ``Z`` suffix; Python 3.9's fromisoformat (which
    # backs detect.parse_iso) accepts the equivalent explicit UTC offset only.
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = detect.parse_iso(candidate)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CloudStatusError(f"GitHub Actions API returned an invalid {field}")
    return detect.as_aware(parsed)


def _query_timestamp(value: datetime) -> str:
    utc = detect.as_aware(value).astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def has_successful_scheduled_run(
    repository: str,
    workflow: str,
    *,
    since: datetime,
    until: datetime,
    timeout: float = 10.0,
) -> bool:
    """Return whether the complete bounded window contains a recent success.

    The repository is public, so this deliberately uses no credential. The API
    does not promise a useful ordering, therefore every returned row is
    validated and inspected. One schedule interval of created-time lookback
    includes a run that started just before ``since`` but completed after it.
    A response that cannot prove it is the complete requested page is unknown,
    represented by :class:`CloudStatusError`, never by ``False``.
    """
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise CloudStatusError("cloud repository must be owner/name")
    if not workflow:
        raise CloudStatusError("cloud workflow must not be empty")

    since = detect.as_aware(since)
    until = detect.as_aware(until)
    if since >= until:
        raise CloudStatusError("cloud health window must have positive duration")
    created_since = since - SCHEDULE_INTERVAL
    window_seconds = (until - created_since).total_seconds()
    per_page = math.ceil(window_seconds / SCHEDULE_INTERVAL.total_seconds()) + 1
    if per_page > MAX_RUNS_PER_PAGE:
        raise CloudStatusError("cloud health window exceeds one trustworthy page")

    owner, name = (quote(part, safe="") for part in parts)
    workflow_id = quote(workflow, safe="")
    url = (
        f"{GITHUB_API_ROOT}/repos/{owner}/{name}/actions/"
        f"workflows/{workflow_id}/runs"
    )
    try:
        response = httpx.get(
            url,
            params={
                "event": "schedule",
                "status": "success",
                "created": (
                    f"{_query_timestamp(created_since)}..{_query_timestamp(until)}"
                ),
                "per_page": per_page,
            },
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

    if not isinstance(body, dict):
        raise CloudStatusError("GitHub Actions API returned an invalid response")
    runs = body.get("workflow_runs")
    total_count = body.get("total_count")
    if (
        not isinstance(runs, list)
        or isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
        or total_count < len(runs)
        or len(runs) > per_page
    ):
        raise CloudStatusError("GitHub Actions API returned an invalid run page")

    successful = False
    run_ids = set()
    for run in runs:
        if not isinstance(run, dict):
            raise CloudStatusError("GitHub Actions API returned an invalid run")
        run_id = run.get("id")
        if (
            isinstance(run_id, bool)
            or not isinstance(run_id, int)
            or run_id <= 0
            or run_id in run_ids
        ):
            raise CloudStatusError("GitHub Actions API returned an invalid run id")
        run_ids.add(run_id)
        if (
            run.get("event") != "schedule"
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
        ):
            raise CloudStatusError("GitHub Actions API returned a contradictory run")
        created = _github_timestamp(run.get("created_at"), "creation time")
        completed = _github_timestamp(run.get("updated_at"), "completion time")
        if not (created_since <= created <= until) or not (created <= completed <= until):
            raise CloudStatusError("GitHub Actions API returned a run outside the window")
        if completed >= since:
            successful = True
    if successful:
        return True
    if total_count != len(runs) or total_count > per_page:
        raise CloudStatusError("GitHub Actions API returned an incomplete run page")
    return False
