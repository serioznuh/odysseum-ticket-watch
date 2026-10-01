"""Snapshot tap (OTW-38): keep what each Pathé poll saw, for the v2 shadow.

The v2 shadow must replay v1's decisions on the same inputs without adding
Pathé traffic, and v1 used to drop every `detect.Snapshot` once analysed. Each
poll of a real run now leaves a record under the git-ignored
`.cache/pathe-snapshots/`:

* a healthy or degraded poll whose content differs from the newest saved file
  writes one gzipped JSON file named after its poll time in UTC
  (`20261001T100000Z.json.gz`, so names sort chronologically across DST);
* an unchanged poll appends `<poll time> same <file>` to `index.log`;
* a failed poll appends `<poll time> failed: <error summary>` to `index.log`,
  so a replay also reproduces the blind spells.

Only Pathé response bodies and the fetch diagnostics are written: nothing from
the environment, the configuration or the state, so no credential can reach
these files. Files older than `RETENTION_DAYS` are deleted (except the newest
of them, which later `same` lines still refer to), old index lines are trimmed,
a temporary file a killed firing left behind is removed, and nothing more is
written once the directory reaches `SIZE_CAP_BYTES`.

Everything here may raise. The caller in `watcher/jobs.py` swallows it with a
warning: the tap must never change an alert, the state or the exit status.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import alerts, detect

log = logging.getLogger("watcher.tap")

# Relative to the repository root, like every other `.cache/` path the watcher
# uses (both wrappers run from there).
SNAPSHOT_DIR = Path(".cache/pathe-snapshots")
INDEX_NAME = "index.log"
RETENTION_DAYS = 30
# index.log is rewritten only once its oldest line is this much past retention,
# so trimming costs one rewrite a day rather than one per poll.
INDEX_TRIM_SLACK = timedelta(days=1)
SIZE_CAP_BYTES = 200 * 1024 * 1024
MAX_SUMMARY = 300
# Older than any write in progress could be: one firing is capped well below.
STALE_TMP_SECONDS = 3600
FORMAT_VERSION = 1

NAME_FORMAT = "%Y%m%dT%H%M%SZ"
SUFFIX = ".json.gz"
_NAME_RE = re.compile(r"^\d{8}T\d{6}Z\.json\.gz$")


# ----------------------------------------------------------------- content

def _result_record(result: detect.FetchResult) -> dict:
    return {
        "health": result.health.value,
        "diagnostic": result.diagnostic,
        "data": result.data,
    }


def snapshot_content(snap: detect.Snapshot) -> dict:
    """Everything a replay needs to rebuild `snap`, poll time excluded."""
    return {
        "matched_shows": snap.matched_shows,
        "cinema_programme": snap.cinema_programme,
        "cinema_entries": snap.cinema_entries,
        "showtimes": snap.showtimes,
        "listing_results": {
            slug: {endpoint: _result_record(r) for endpoint, r in endpoints.items()}
            for slug, endpoints in snap.listing_results.items()
        },
        "unreadable_metadata": snap.unreadable_metadata,
    }


def content_digest(content: dict) -> str:
    canonical = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_from_content(content: dict) -> detect.Snapshot:
    return detect.Snapshot(
        matched_shows=content["matched_shows"],
        cinema_entries=content["cinema_entries"],
        showtimes=content["showtimes"],
        listing_results={
            slug: {
                endpoint: detect.FetchResult(
                    data=r["data"],
                    health=detect.FetchHealth(r["health"]),
                    diagnostic=r["diagnostic"],
                )
                for endpoint, r in endpoints.items()
            }
            for slug, endpoints in content["listing_results"].items()
        },
        unreadable_metadata=content["unreadable_metadata"],
        cinema_programme=content["cinema_programme"],
    )


def read_file(path: str | Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        record = json.load(fh)
    if record.get("version") != FORMAT_VERSION:
        raise ValueError(f"{path}: unsupported snapshot format {record.get('version')!r}")
    return record


def load_snapshot(path: str | Path) -> detect.Snapshot:
    """Rebuild the `detect.Snapshot` a saved poll held."""
    return snapshot_from_content(read_file(path)["snapshot"])


# --------------------------------------------------------------- directory

def _file_time(name: str) -> datetime:
    return datetime.strptime(name[: -len(SUFFIX)], NAME_FORMAT).replace(
        tzinfo=timezone.utc
    )


def _snapshot_files(directory: Path) -> list[Path]:
    """Saved polls, oldest first (names sort chronologically)."""
    return sorted(p for p in directory.iterdir() if _NAME_RE.match(p.name))


def _line_time(line: str) -> datetime | None:
    parsed = detect.parse_iso(line.split(" ", 1)[0])
    return detect.as_aware(parsed) if parsed else None


def _atomic_write(directory: Path, name: str, data: bytes) -> None:
    """Write via a temporary file and a rename, so a reader never sees half."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, directory / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rotate(directory: Path, now: datetime) -> None:
    cutoff = now - timedelta(days=RETENTION_DAYS)
    expired = [p for p in _snapshot_files(directory) if _file_time(p.name) < cutoff]
    # The newest expired file stays: it is the content every `same` line
    # between the cutoff and the next saved file refers to.
    for path in expired[:-1]:
        path.unlink()
    # A firing killed mid-write leaves its temporary file behind; nothing else
    # would ever remove it, and it would count against the cap for good.
    stale_tmp = time.time() - STALE_TMP_SECONDS
    for path in directory.glob(".*.tmp"):
        if path.stat().st_mtime < stale_tmp:
            path.unlink()

    index = directory / INDEX_NAME
    if not index.exists():
        return
    with open(index, encoding="utf-8") as fh:
        first = fh.readline()
    oldest = _line_time(first) if first else None
    if not first or (oldest is not None and oldest >= cutoff - INDEX_TRIM_SLACK):
        return
    lines = index.read_text(encoding="utf-8").splitlines()
    kept = [
        line for line in lines
        if (when := _line_time(line)) is not None and when >= cutoff
    ]
    _atomic_write(
        directory, INDEX_NAME, "".join(f"{line}\n" for line in kept).encode("utf-8")
    )


def _directory_size(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.iterdir() if p.is_file())


def _prepare(directory: str | Path | None, now: datetime) -> Path | None:
    """Ready the directory; None when it is full and nothing may be written."""
    target = Path(directory) if directory is not None else SNAPSHOT_DIR
    target.mkdir(parents=True, exist_ok=True)
    _rotate(target, now)
    size = _directory_size(target)
    if size >= SIZE_CAP_BYTES:
        log.warning(
            "Pathé snapshot tap: %s holds %.0f MB (cap %.0f MB) — this poll is not recorded",
            target,
            size / 1024 / 1024,
            SIZE_CAP_BYTES / 1024 / 1024,
        )
        return None
    return target


def _append_index(directory: Path, now: datetime, text: str) -> None:
    with open(directory / INDEX_NAME, "a", encoding="utf-8") as fh:
        fh.write(f"{now.isoformat(timespec='seconds')} {text}\n")


def _previous_digest(directory: Path) -> tuple[str | None, str | None]:
    files = _snapshot_files(directory)
    if not files:
        return None, None
    newest = files[-1]
    try:
        return newest.name, read_file(newest).get("digest")
    except (OSError, ValueError, EOFError):
        # An unreadable newest file is no baseline: the next file stands alone.
        return newest.name, None


# ------------------------------------------------------------------ record

def record_poll(
    snap: detect.Snapshot, now: datetime, directory: str | Path | None = None
) -> str | None:
    """Record one healthy or degraded poll.

    Returns the saved file's name, ``"same"`` when the content matched the
    newest file, or None when the directory was full.
    """
    now = detect.as_aware(now)
    target = _prepare(directory, now)
    if target is None:
        return None
    content = snapshot_content(snap)
    digest = content_digest(content)
    previous, previous_digest = _previous_digest(target)
    if previous_digest == digest:
        _append_index(target, now, f"same {previous}")
        return "same"
    record = {
        "version": FORMAT_VERSION,
        "poll_time": now.isoformat(timespec="seconds"),
        "health": "healthy" if snap.healthy else "degraded",
        "digest": digest,
        "snapshot": content,
    }
    name = now.astimezone(timezone.utc).strftime(NAME_FORMAT) + SUFFIX
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
    _atomic_write(target, name, gzip.compress(payload))
    return name


def record_failure(
    error: Any, now: datetime, directory: str | Path | None = None
) -> bool:
    """Record one failed poll as a single index line. False when full."""
    now = detect.as_aware(now)
    target = _prepare(directory, now)
    if target is None:
        return False
    summary, _status = alerts.summarize_pathe_error(str(error))
    # One bounded line per failure, whatever the exception text looked like.
    summary = " ".join(summary.split())[:MAX_SUMMARY] or "unknown error"
    _append_index(target, now, f"failed: {summary}")
    return True
