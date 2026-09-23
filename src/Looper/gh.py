"""Every GitHub interaction goes through the `gh` CLI, and through this module."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import Any

log = logging.getLogger("looper.gh")

RETRYABLE = ("rate limit", "was submitted too quickly", "abuse detection",
             "502", "503", "504", "timeout", "connection reset")


class GHError(RuntimeError):
    def __init__(self, args: list[str], code: int, stderr: str):
        super().__init__(f"gh {' '.join(args)} failed ({code}): {stderr.strip()[:500]}")
        self.args_ = args
        self.code = code
        self.stderr = stderr


class GH:
    """Thin, retrying wrapper around `gh`. All methods are blocking; callers on the
    event loop should wrap them in ``asyncio.to_thread``."""

    def __init__(self, slug: str, *, dry_run: bool = False, bin: str = "gh",
                 max_retries: int = 4, timeout: int = 120):
        self.slug = slug
        self.dry_run = dry_run
        self.bin = bin
        self.max_retries = max_retries
        self.timeout = timeout

    # --- plumbing --------------------------------------------------------
    def _run(self, args: list[str], *, parse_json: bool = False, check: bool = True) -> Any:
        delay = 2.0
        last: GHError | None = None
        for attempt in range(self.max_retries):
            proc = subprocess.run(
                [self.bin, *args], capture_output=True, text=True, timeout=self.timeout
            )
            if proc.returncode == 0:
                out = proc.stdout.strip()
                if not parse_json:
                    return out
                return json.loads(out) if out else None
            err = proc.stderr or proc.stdout
            last = GHError(args, proc.returncode, err)
            if not any(token in err.lower() for token in RETRYABLE):
                break
            log.warning("gh retry %d/%d after: %s", attempt + 1, self.max_retries, err.strip()[:200])
            time.sleep(delay)
            delay *= 2
        if check and last:
            raise last
        return None

    def _mutate(self, args: list[str], what: str, *, parse_json: bool = False) -> Any:
        if self.dry_run:
            log.info("[dry-run] would %s: gh %s", what, " ".join(args))
            return None
        return self._run(args, parse_json=parse_json)

    def api(self, path: str, *, paginate: bool = False, method: str | None = None,
            fields: dict[str, str] | None = None) -> Any:
        args = ["api", path]
        if paginate:
            args.append("--paginate")
        if method:
            args += ["--method", method]
        for k, v in (fields or {}).items():
            args += ["-f", f"{k}={v}"]
        if paginate:
            # --paginate --slurp merges pages into one JSON array
            args.append("--slurp")
            pages = self._run(args, parse_json=True) or []
            merged: list[Any] = []
            for page in pages:
                merged.extend(page if isinstance(page, list) else [page])
            return merged
        return self._run(args, parse_json=True)

    # --- issues ----------------------------------------------------------
    def list_open_issues(self, limit: int = 100) -> list[dict[str, Any]]:
        out = self._run(
            ["issue", "list", "--repo", self.slug, "--state", "open", "--limit", str(limit),
             "--json", "number,title,body,labels,updatedAt,author,assignees,url"],
            parse_json=True,
        )
        return out or []

    def issue(self, number: int) -> dict[str, Any]:
        return self._run(
            ["issue", "view", str(number), "--repo", self.slug,
             "--json", "number,title,body,labels,state,author,url,comments"],
            parse_json=True,
        )

    def comment(self, number: int, body: str) -> None:
        self._mutate(["issue", "comment", str(number), "--repo", self.slug, "--body", body],
                     f"comment on #{number}")

    def add_label(self, number: int, label: str) -> None:
        self.ensure_label(label)
        self._mutate(["issue", "edit", str(number), "--repo", self.slug, "--add-label", label],
                     f"label #{number} {label}")

    def remove_label(self, number: int, label: str) -> None:
        self._remove_label("issue", number, label)

    def _remove_label(self, kind: str, number: int, label: str) -> None:
        """A label that doesn't exist is already removed; any other failure raises."""
        try:
            self._mutate([kind, "edit", str(number), "--repo", self.slug, "--remove-label", label],
                         f"unlabel {kind} #{number} {label}")
        except GHError as exc:
            if "not found" not in exc.stderr.lower():
                raise
            log.debug("remove %s label ignored: %s", kind, exc)

    def ensure_label(self, label: str, color: str = "5319e7") -> None:
        if self.dry_run:
            return
        try:
            self._run(["label", "create", label, "--repo", self.slug, "--color", color,
                       "--description", "managed by BarebonesHarness"])
        except GHError as exc:
            if "already exists" not in exc.stderr.lower():
                log.debug("ensure_label(%s): %s", label, exc)

    # --- pull requests ---------------------------------------------------
    def create_pr(self, *, head: str, base: str, title: str, body: str) -> int | None:
        url = self._mutate(
            ["pr", "create", "--repo", self.slug, "--base", base, "--head", head,
             "--title", title, "--body", body],
            f"open PR {head} -> {base}",
        )
        if not url:
            return None
        return int(str(url).rstrip("/").rsplit("/", 1)[-1])

    def find_pr_for_branch(self, branch: str) -> dict[str, Any] | None:
        out = self._run(
            ["pr", "list", "--repo", self.slug, "--head", branch, "--state", "all",
             "--json", "number,state,url,headRefOid"],
            parse_json=True,
        ) or []
        return out[0] if out else None

    def pr(self, number: int) -> dict[str, Any]:
        return self._run(
            ["pr", "view", str(number), "--repo", self.slug,
             "--json", "number,state,url,headRefOid,mergeable,statusCheckRollup,title"],
            parse_json=True,
        )

    def pr_issue_comments(self, number: int) -> list[dict[str, Any]]:
        """Top-level PR conversation comments — where Greptile posts its summary + score."""
        return self.api(f"repos/{self.slug}/issues/{number}/comments?per_page=100", paginate=True)

    def pr_reviews(self, number: int) -> list[dict[str, Any]]:
        return self.api(f"repos/{self.slug}/pulls/{number}/reviews?per_page=100", paginate=True)

    def pr_inline_comments(self, number: int) -> list[dict[str, Any]]:
        return self.api(f"repos/{self.slug}/pulls/{number}/comments?per_page=100", paginate=True)

    def pr_comment(self, number: int, body: str) -> None:
        self._mutate(["pr", "comment", str(number), "--repo", self.slug, "--body", body],
                     f"comment on PR #{number}")

    def check_runs(self, sha: str) -> list[dict[str, Any]]:
        out = self.api(f"repos/{self.slug}/commits/{sha}/check-runs?per_page=100")
        return (out or {}).get("check_runs", [])

    def failed_job_log(self, job_id: str) -> str:
        """Log of the failed steps of one Actions job; '' if GitHub won't give it up."""
        try:
            return self._run(["run", "view", "--job", job_id, "--log-failed", "--repo", self.slug]) or ""
        except GHError as exc:
            log.debug("failed_job_log(%s): %s", job_id, exc)
            return ""

    # --- misc ------------------------------------------------------------
    def default_branch(self) -> str:
        out = self._run(["repo", "view", self.slug, "--json", "defaultBranchRef"], parse_json=True)
        return (out or {}).get("defaultBranchRef", {}).get("name", "main")

    def whoami(self) -> str:
        return self._run(["api", "user", "--jq", ".login"])

    def preflight(self) -> None:
        """Fail fast with a readable message rather than mid-run."""
        self._run(["auth", "status"])
        self._run(["repo", "view", self.slug, "--json", "name"], parse_json=True)

    def edit_pr_body(self, number: int, body: str) -> None:
        self._mutate(["pr", "edit", str(number), "--repo", self.slug, "--body", body],
                     f"rewrite body of PR #{number}")

    def add_pr_label(self, number: int, label: str) -> None:
        self.ensure_label(label)
        self._mutate(["pr", "edit", str(number), "--repo", self.slug, "--add-label", label],
                     f"label PR #{number} {label}")

    def remove_pr_label(self, number: int, label: str) -> None:
        self._remove_label("pr", number, label)
