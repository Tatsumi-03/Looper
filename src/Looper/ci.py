"""Reading CI off a PR: `ready` means Greptile is happy *and* the checks are green.

`gh pr view --json statusCheckRollup` merges both flavours GitHub has — check runs
(Actions, most apps) and legacy commit statuses — for the PR's head commit.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .gh import GH

log = logging.getLogger("looper.ci")

# anything else (SUCCESS, NEUTRAL, SKIPPED, CANCELLED, ...) is nothing the agent can fix
FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ERROR"}
PENDING = {"PENDING", "EXPECTED"}  # StatusContext states; CheckRuns use status != COMPLETED
JOB_RE = re.compile(r"/actions/runs/\d+/job/(\d+)")
LOG_TAIL = 150


def verdict(rollup: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
    """(anything still running?, the checks that failed)."""
    pending, failing = False, []
    for c in rollup or []:
        if c.get("__typename") == "StatusContext":
            state = c.get("state", "")
            pending |= state in PENDING
        else:
            pending |= c.get("status") != "COMPLETED"
            state = c.get("conclusion") or ""
        if state in FAILED:
            failing.append(c)
    return pending, failing


async def wait_for_ci(gh: GH, pr: int, *, head_sha: str | None, timeout_sec: int,
                      poll_sec: int) -> dict[str, Any] | None:
    """The PR view once CI on `head_sha` settles (or the PR conflicts), None on timeout.

    A conflicting PR settles at once: GitHub won't run pull_request workflows on it.

    ponytail: a repo whose CI hasn't registered any checks yet reads as green. We only
    get here after Greptile has taken minutes to score, so CI has long since started.
    """
    waited = 0
    while True:
        view = await asyncio.to_thread(gh.pr, pr) or {}
        fresh = not view.get("headRefOid") or view["headRefOid"] == head_sha
        pending, _ = verdict(view.get("statusCheckRollup") or [])
        if fresh and (not pending or view.get("mergeable") == "CONFLICTING"):
            return view
        if waited >= timeout_sec:
            return None
        await asyncio.sleep(poll_sec)
        waited += max(1, poll_sec)
        if waited % 60 < max(1, poll_sec):
            log.info("PR #%s: waiting on CI — %dm of %dm", pr, waited // 60, timeout_sec // 60)


def render(gh: GH, failing: list[dict[str, Any]], head_sha: str | None) -> str:
    parts = [f"## CI is failing on {(head_sha or '')[:7]}"]
    for c in failing:
        name = c.get("name") or c.get("context") or "check"
        url = c.get("detailsUrl") or c.get("targetUrl") or ""
        parts.append(f"### {name} — {c.get('conclusion') or c.get('state')}\n{url}")
        if m := JOB_RE.search(url):
            tail = "\n".join(gh.failed_job_log(m.group(1)).splitlines()[-LOG_TAIL:])
            if tail:
                parts.append(f"```\n{tail}\n```")
    return "\n\n".join(parts)
