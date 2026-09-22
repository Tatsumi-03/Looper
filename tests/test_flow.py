"""End-to-end FSM test: real git worktrees + pushes, fake GitHub, fake agent."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from Looper.agent import RunResult
from Looper.config import AgentCfg, Config, GreptileCfg, LoopCfg, RepoCfg, SafetyCfg
from Looper.orchestrator import LABEL_NEEDS_HUMAN, LABEL_READY, Orchestrator
from Looper.state import State, Store
from Looper.worktree import Worktrees


def git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def origin(tmp_path):
    """A bare repo standing in for GitHub, with one commit on main."""
    bare = tmp_path / "origin.git"
    git(["init", "--bare", "--initial-branch=main", str(bare)], tmp_path)
    seed = tmp_path / "seed"
    git(["clone", str(bare), str(seed)], tmp_path)
    git(["config", "user.email", "t@t"], seed)
    git(["config", "user.name", "t"], seed)
    (seed / "README.md").write_text("# seed\n")
    git(["add", "-A"], seed)
    git(["commit", "-m", "init"], seed)
    git(["push", "-u", "origin", "main"], seed)
    return bare


class FakeGH:
    """Records every mutation so the test can assert on GitHub side effects."""

    def __init__(self):
        self.prs, self.comments, self.labels, self.calls = {}, [], [], []
        self._next_pr = 100

    def preflight(self):
        pass

    def issue(self, n):
        return {"number": n, "title": f"Fix thing {n}", "body": "It is broken.", "comments": []}

    def add_label(self, n, label):
        self.labels.append((n, label))

    def add_pr_label(self, n, label):
        self.labels.append((n, label))

    def remove_label(self, n, label):
        self.labels.append((n, f"-{label}"))

    def comment(self, n, body):
        self.comments.append((n, body))

    def pr_comment(self, n, body):
        self.comments.append((n, body))

    def find_pr_for_branch(self, branch):
        return next((p for p in self.prs.values() if p["head"] == branch), None)

    def create_pr(self, *, head, base, title, body):
        self._next_pr += 1
        self.prs[self._next_pr] = {"number": self._next_pr, "head": head, "base": base,
                                   "title": title, "body": body, "state": "OPEN"}
        return self._next_pr

    def pr_issue_comments(self, n):
        return []

    def pr_reviews(self, n):
        return []

    def pr_inline_comments(self, n):
        return []


class FakeAgent:
    """Stands in for Claude Code: appends a line and commits, unless told otherwise."""

    def __init__(self, *, abort=False, noop=False):
        self.abort, self.noop = abort, noop
        self.runs = []

    async def run(self, prompt, *, cwd, log_path, session_id=None, resume=False):
        self.runs.append({"prompt": prompt, "resume": resume, "cwd": Path(cwd)})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("{}\n")
        if self.abort:
            return RunResult(True, session_id, 0.01, 1, "HARNESS_ABORT: too vague",
                             "success", None, log_path, 0.1)
        if not self.noop:
            target = Path(cwd) / "fix.txt"
            target.write_text(target.read_text() + "x\n" if target.exists() else "x\n")
            git(["add", "-A"], cwd)
            git(["commit", "-m", f"fix: round {len(self.runs)}"], cwd)
        return RunResult(True, session_id, 0.25, 3, "done", "success", None, log_path, 1.0)


def make(tmp_path, origin, *, scores, max_iterations=5):
    cfg = Config(
        repo=RepoCfg(slug="acme/widget", base_branch="main"),
        agent=AgentCfg(model="sonnet"),
        loop=LoopCfg(max_concurrent_agents=1, max_iterations=max_iterations, max_open_prs=5),
        greptile=GreptileCfg(fake_scores=scores, target_score=5, poll_interval_sec=0),
        safety=SafetyCfg(branch_prefix="agent/issue-"),
        root=tmp_path / "work",
    )
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    gh = FakeGH()
    wt = Worktrees("acme/widget", base_branch="main", repos_dir=cfg.repos_dir,
                   worktrees_dir=cfg.worktrees_dir, branch_prefix="agent/issue-",
                   clone_url=str(origin))
    return cfg, store, gh, wt


def test_happy_path_iterates_until_target_score(tmp_path, origin):
    cfg, store, gh, wt = make(tmp_path, origin, scores=[3, 5])
    agent = FakeAgent()
    orch = Orchestrator(cfg, store, gh, wt, agent)

    task = asyncio.run(orch.drive(42))

    assert task.state == State.READY_FOR_HUMAN
    assert task.pr_number == 101 and task.last_score == 5 and task.best_score == 5
    assert task.iteration == 1, "one revision round"
    assert task.cost_usd == pytest.approx(0.5)

    # one solve + one revise, the revise resumed the same session
    assert [r["resume"] for r in agent.runs] == [False, True]
    assert "Greptile review" in agent.runs[1]["prompt"]

    # the branch really exists on the fake origin, with both commits
    out = subprocess.run(["git", "log", "--oneline", "agent/issue-42"], cwd=origin,
                         capture_output=True, text=True, check=True).stdout
    assert out.count("fix: round") == 2
    assert (42, LABEL_READY) not in gh.labels and (101, LABEL_READY) in gh.labels
    assert any("5/5" in body for _n, body in gh.comments)


def test_abort_parks_without_opening_a_pr(tmp_path, origin):
    cfg, store, gh, wt = make(tmp_path, origin, scores=[5])
    orch = Orchestrator(cfg, store, gh, wt, FakeAgent(abort=True))

    task = asyncio.run(orch.drive(7))

    assert task.state == State.PARKED
    assert "too vague" in task.error and task.pr_number is None
    assert gh.prs == {}
    assert (7, LABEL_NEEDS_HUMAN) in gh.labels


def test_max_iterations_parks_with_best_score(tmp_path, origin):
    cfg, store, gh, wt = make(tmp_path, origin, scores=[2, 4], max_iterations=2)
    orch = Orchestrator(cfg, store, gh, wt, FakeAgent())

    task = asyncio.run(orch.drive(8))

    assert task.state == State.PARKED
    assert "max_iterations=2" in task.error
    assert task.best_score == 4 and task.pr_number == 101
    assert (101, LABEL_NEEDS_HUMAN) in gh.labels


def test_restart_resumes_after_pr_without_resolving_again(tmp_path, origin):
    cfg, store, gh, wt = make(tmp_path, origin, scores=[3, 5])
    first = FakeAgent()
    asyncio.run(Orchestrator(cfg, store, gh, wt, first).drive(42))

    # simulate a fresh daemon on the same DB: task is terminal, nothing re-runs
    second = FakeAgent()
    task = asyncio.run(Orchestrator(cfg, store, gh, wt, second).drive(42))
    assert task.state == State.READY_FOR_HUMAN and second.runs == []

    # now reopen the review loop as if the daemon died mid-flight
    store.set_state(42, State.AWAITING_REVIEW, "simulated crash", error=None)
    cfg.greptile.fake_scores = [5]
    third = FakeAgent()
    task = asyncio.run(Orchestrator(cfg, store, gh, wt, third).drive(42))
    assert task.state == State.READY_FOR_HUMAN
    assert third.runs == [], "resuming after the PR must not re-solve the issue"
    assert len(gh.prs) == 1, "no duplicate PR"


def test_agent_writes_the_pr_description(tmp_path, origin):
    """The agent owns the PR body — some repos' review rules live there."""

    class BodyAgent(FakeAgent):
        async def run(self, prompt, *, cwd, log_path, session_id=None, resume=False):
            result = await super().run(prompt, cwd=cwd, log_path=log_path,
                                       session_id=session_id, resume=resume)
            round_no = len(self.runs)
            result.result_text = (
                f"done\n<pr-description>\n## Round {round_no}\nEvidence: it works.\n"
                "</pr-description>")
            return result

    cfg, store, gh, wt = make(tmp_path, origin, scores=[3, 5])
    edits = []
    gh.edit_pr_body = lambda n, body: edits.append((n, body))

    task = asyncio.run(Orchestrator(cfg, store, gh, wt, BodyAgent()).drive(42))

    assert task.state == State.READY_FOR_HUMAN
    created = gh.prs[101]["body"]
    assert "Closes #42" in created and "## Round 1" in created and "Evidence: it works." in created
    assert edits and edits[0][0] == 101 and "## Round 2" in edits[0][1], \
        "the revision round must rewrite the description"


def test_pending_pr_body_does_not_leak_between_issues(tmp_path, origin):
    cfg, store, gh, wt = make(tmp_path, origin, scores=[5])
    orch = Orchestrator(cfg, store, gh, wt, FakeAgent())
    assert orch._extract_pr_body("blah <pr-description>\nhi\n</pr-description> tail") == "hi"
    assert orch._extract_pr_body("no block here") is None

    orch._pending_pr_body[1] = "body for 1"
    assert orch._pending_pr_body.pop(2, None) is None
    assert orch._pending_pr_body[1] == "body for 1"


def test_concurrent_worktrees_do_not_race_on_the_shared_clone(tmp_path, origin):
    """Every worktree shares one clone; concurrent fetches used to collide on
    refs/remotes/origin/main and park the task with a GitError."""
    import threading

    cfg, store, gh, wt = make(tmp_path, origin, scores=[5])
    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def worker(n: int) -> None:
        try:
            barrier.wait(timeout=30)
            path = wt.ensure_worktree(n)
            assert path.exists()
        except Exception as exc:  # noqa: BLE001 - collected for the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20, 26)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"concurrent worktree creation failed: {errors}"
    assert sorted(p.name for p in cfg.worktrees_dir.iterdir()) == [
        f"issue-{n}" for n in range(20, 26)]


def test_crashed_solve_resumes_its_session_instead_of_reusing_the_id(tmp_path, origin):
    """A task left in SOLVING already owns a claude session; re-running must resume it."""
    cfg, store, gh, wt = make(tmp_path, origin, scores=[5])
    store.create(31, "half-done")
    store.set_state(31, State.SOLVING, session_id="pre-existing-session")

    agent = FakeAgent()
    asyncio.run(Orchestrator(cfg, store, gh, wt, agent).drive(31))

    assert agent.runs[0]["resume"] is True, "must resume, not re-claim the session id"


def test_failed_run_over_stale_commits_parks_instead_of_pushing(tmp_path, origin):
    """Leftover commits from an earlier attempt must not become a PR on their own."""

    class BrokenAgent(FakeAgent):
        async def run(self, prompt, *, cwd, log_path, session_id=None, resume=False):
            self.runs.append({"prompt": prompt, "resume": resume, "cwd": Path(cwd)})
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("{}\n")
            return RunResult(False, session_id, 0.0, 0, "",
                             None, "Session ID x is already in use.", log_path, 0.1)

    cfg, store, gh, wt = make(tmp_path, origin, scores=[5])
    # leave a commit behind, as a killed agent would
    path = wt.ensure_worktree(32)
    (path / "stale.txt").write_text("from an earlier attempt\n")
    git(["add", "-A"], path)
    git(["commit", "-m", "stale work"], path)

    task = asyncio.run(Orchestrator(cfg, store, gh, wt, BrokenAgent()).drive(32))

    assert task.state == State.PARKED
    assert "added nothing" in task.error
    assert gh.prs == {}, "no PR may be opened from another run's commits"
