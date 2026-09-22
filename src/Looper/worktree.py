"""Git plumbing: one base clone, one worktree per issue, guarded pushes."""

from __future__ import annotations

import fcntl
import logging
import shutil
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("looper.git")


class GitError(RuntimeError):
    pass


class PushGuardError(GitError):
    """Raised when something tries to push outside the agent branch namespace."""


def _git(args: list[str], cwd: Path | None = None, *, check: bool = True,
         timeout: int = 300) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): "
                       f"{(proc.stderr or proc.stdout).strip()[:500]}")
    return proc


@contextmanager
def file_lock(path: Path, *, blocking: bool = False):
    """Advisory lock so two looper processes never touch the same worktree."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise GitError(f"{path} is locked by another looper process") from exc
    try:
        yield fh
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


class Worktrees:
    def __init__(self, slug: str, *, base_branch: str, repos_dir: Path, worktrees_dir: Path,
                 branch_prefix: str, dry_run: bool = False, clone_url: str | None = None):
        self.slug = slug
        self.clone_url = clone_url
        self.base_branch = base_branch
        self.repos_dir = Path(repos_dir)
        self.worktrees_dir = Path(worktrees_dir)
        self.branch_prefix = branch_prefix
        self.dry_run = dry_run
        # Every worktree shares one clone, so its refs and config are a contended
        # resource: concurrent `git fetch` calls race on refs/remotes/origin/*.
        self._rlock = threading.RLock()
        self._depth = 0
        self._lock_fh = None

    @contextmanager
    def _repo_lock(self):
        """Serialize operations on the shared clone, in-process and across processes.

        Re-entrant: the outermost caller holds the flock, so nested helpers
        (ensure_worktree -> fetch -> ensure_base) do not deadlock on themselves.
        """
        with self._rlock:
            if self._depth == 0:
                self.repos_dir.mkdir(parents=True, exist_ok=True)
                self._lock_fh = (self.repos_dir / f"{self.slug.replace('/', '__')}.lock").open("a+")
                fcntl.flock(self._lock_fh, fcntl.LOCK_EX)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._lock_fh is not None:
                    fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                    self._lock_fh.close()
                    self._lock_fh = None

    @property
    def base(self) -> Path:
        return self.repos_dir / self.slug.replace("/", "__")

    def path_for(self, issue: int) -> Path:
        return self.worktrees_dir / f"issue-{issue}"

    # --- base clone ------------------------------------------------------
    def ensure_base(self) -> Path:
        if (self.base / ".git").exists():
            return self.base
        with self._repo_lock():
            return self._clone()

    def _clone(self) -> Path:
        if (self.base / ".git").exists():  # another worker won the race
            return self.base
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        log.info("cloning %s -> %s", self.clone_url or self.slug, self.base)
        cmd = (["git", "clone", self.clone_url, str(self.base)] if self.clone_url
               else ["gh", "repo", "clone", self.slug, str(self.base)])
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            raise GitError(f"clone failed: {(proc.stderr or proc.stdout).strip()[:500]}")
        if not self.clone_url:
            # push/fetch auth without relying on a global `gh auth setup-git`
            _git(["config", "credential.https://github.com.helper", "!gh auth git-credential"],
                 cwd=self.base)
        if not _git(["config", "user.email"], cwd=self.base, check=False).stdout.strip():
            _git(["config", "user.email", "looper@local"], cwd=self.base)
            _git(["config", "user.name", "BarebonesHarness"], cwd=self.base)
        return self.base

    def fetch(self) -> None:
        self.ensure_base()
        with self._repo_lock():
            _git(["fetch", "--prune", "origin"], cwd=self.base)

    def remote_branch_exists(self, branch: str) -> bool:
        out = _git(["ls-remote", "--heads", "origin", branch], cwd=self.base).stdout
        return bool(out.strip())

    # --- per-issue worktrees --------------------------------------------
    def ensure_worktree(self, issue: int) -> Path:
        """Create (or reattach) the worktree for an issue. Idempotent: an existing
        worktree already on the right branch is reused so a restart keeps local work."""
        branch = self.branch_for(issue)
        path = self.path_for(issue)
        with self._repo_lock():
            return self._add_worktree(issue, branch, path)

    def _add_worktree(self, issue: int, branch: str, path: Path) -> Path:
        self.fetch()

        if path.exists():
            current = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path, check=False)
            if current.returncode == 0 and current.stdout.strip() == branch:
                log.info("reusing worktree %s on %s", path, branch)
                return path
            log.warning("worktree %s is not on %s — recreating", path, branch)
            self.remove(issue)

        _git(["worktree", "prune"], cwd=self.base)
        start = f"origin/{branch}" if self.remote_branch_exists(branch) else f"origin/{self.base_branch}"
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(["worktree", "add", "-B", branch, str(path), start], cwd=self.base)
        log.info("worktree %s on %s (from %s)", path, branch, start)
        return path

    def branch_for(self, issue: int) -> str:
        return f"{self.branch_prefix}{issue}"

    def remove(self, issue: int) -> None:
        path = self.path_for(issue)
        if not path.exists():
            return
        with self._repo_lock():
            self._remove(path)

    def _remove(self, path: Path) -> None:
        proc = _git(["worktree", "remove", "--force", str(path)], cwd=self.base, check=False)
        if proc.returncode != 0 and path.exists():
            shutil.rmtree(path, ignore_errors=True)
        _git(["worktree", "prune"], cwd=self.base, check=False)

    # --- inspection ------------------------------------------------------
    def is_dirty(self, path: Path) -> bool:
        return bool(_git(["status", "--porcelain"], cwd=path).stdout.strip())

    def head_sha(self, path: Path) -> str:
        return _git(["rev-parse", "HEAD"], cwd=path).stdout.strip()

    def commits_ahead(self, path: Path) -> int:
        out = _git(["rev-list", "--count", f"origin/{self.base_branch}..HEAD"], cwd=path).stdout
        return int(out.strip() or 0)

    def diff_stat(self, path: Path) -> str:
        return _git(["diff", "--stat", f"origin/{self.base_branch}...HEAD"], cwd=path).stdout.strip()

    def commit_all(self, path: Path, message: str) -> bool:
        """Safety net: commit anything the agent left uncommitted."""
        if not self.is_dirty(path):
            return False
        _git(["add", "-A"], cwd=path)
        _git(["commit", "-m", message, "--no-verify"], cwd=path)
        return True

    # --- the one mutating remote operation -------------------------------
    def push(self, path: Path, branch: str) -> str:
        if not branch.startswith(self.branch_prefix):
            raise PushGuardError(
                f"refusing to push {branch!r}: does not start with {self.branch_prefix!r}"
            )
        if branch == self.base_branch:
            raise PushGuardError(f"refusing to push the base branch {branch!r}")
        if self.dry_run:
            log.info("[dry-run] would push %s from %s", branch, path)
            return self.head_sha(path)
        # a push writes refs/remotes/origin/<branch> and branch config in the shared clone
        with self._repo_lock():
            _git(["push", "--set-upstream", "origin", f"{branch}:{branch}"], cwd=path, timeout=600)
        return self.head_sha(path)
