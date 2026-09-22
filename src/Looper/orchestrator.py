"""The loop: issue -> worktree -> agent -> PR -> Greptile -> revise -> 5/5."""

from __future__ import annotations

import asyncio
import logging
import re
import signal
import uuid
from pathlib import Path
from typing import Any

from . import greptile
from .agent import ClaudeAgent, RunResult
from .config import Config
from .gh import GH, GHError
from .greptile import ReviewResult
from .state import State, Store, Task, now
from .worktree import GitError, Worktrees, file_lock

log = logging.getLogger("looper.orchestrator")

PROMPTS = Path(__file__).parent / "prompts"
LABEL_IN_PROGRESS = "agent:in-progress"
LABEL_READY = "agent:ready"
LABEL_NEEDS_HUMAN = "agent:needs-human"
ABORT_TOKEN = "HARNESS_ABORT:"
MARKER = "<!-- looper -->"
PR_BODY_RE = re.compile(r"<pr-description>(.*?)</pr-description>", re.S | re.I)


class Parked(Exception):
    """Raised internally to unwind a task into the PARKED state."""


def render(template: str, **vars: Any) -> str:
    text = (PROMPTS / template).read_text()
    for key, value in vars.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def trackable_issues(gh: GH, cfg: Config, store: Store) -> list[dict[str, Any]]:
    """Open issues not skip-labelled and not already claimed by another runner.

    Shared by the daemon's poll loop and the TUI's own issue discovery, so both
    apply the same skip-label / in-progress rules instead of drifting apart.
    """
    out = []
    for issue in gh.list_open_issues():
        labels = {l["name"].lower() for l in issue.get("labels", [])}
        if labels & {s.lower() for s in cfg.loop.skip_labels}:
            continue
        if LABEL_IN_PROGRESS in labels and store.get(issue["number"]) is None:
            log.info("#%s already labelled %s by another runner — skipping",
                     issue["number"], LABEL_IN_PROGRESS)
            continue
        out.append(issue)
    return out


class Orchestrator:
    def __init__(self, cfg: Config, store: Store, gh: GH, wt: Worktrees, agent: ClaudeAgent):
        self.cfg = cfg
        self.store = store
        self.gh = gh
        self.wt = wt
        self.agent = agent
        self.shutdown = asyncio.Event()
        self._sem = asyncio.Semaphore(cfg.loop.max_concurrent_agents)
        self._running: dict[int, asyncio.Task[Any]] = {}
        # per-issue, because several issues are driven concurrently
        self._pending_pr_body: dict[int, str] = {}

    # ------------------------------------------------------------------ #
    # entry points
    # ------------------------------------------------------------------ #
    async def run_once(self, issue_number: int) -> Task | None:
        self.cfg.ensure_dirs()
        await asyncio.to_thread(self.gh.preflight)
        return await self.drive(issue_number)

    async def run_daemon(self) -> None:
        self.cfg.ensure_dirs()
        await asyncio.to_thread(self.gh.preflight)
        self._install_signals()
        log.info("daemon up: repo=%s concurrency=%d target=%d/5 dry_run=%s",
                 self.cfg.repo.slug, self.cfg.loop.max_concurrent_agents,
                 self.cfg.greptile.target_score, self.cfg.safety.dry_run)

        for task in self.store.unfinished():
            log.info("resuming #%s from %s", task.issue_number, task.state)
            self._spawn(task.issue_number)

        while not self.shutdown.is_set():
            try:
                await self._poll_issues()
            except GHError as exc:
                log.error("issue poll failed: %s", exc)
            except Exception:
                log.exception("issue poll blew up")
            try:
                await asyncio.wait_for(self.shutdown.wait(), timeout=self.cfg.loop.issue_poll_sec)
            except asyncio.TimeoutError:
                pass

        log.info("shutting down — waiting for %d in-flight task(s)", len(self._running))
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        log.info("daemon stopped")

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_shutdown, sig)
            except NotImplementedError:  # pragma: no cover
                pass

    def _request_shutdown(self, sig: signal.Signals) -> None:
        if self.shutdown.is_set():
            log.warning("second %s — cancelling in-flight agents", sig.name)
            for t in self._running.values():
                t.cancel()
            return
        log.info("%s received — no new tasks; in-flight agents will finish", sig.name)
        self.shutdown.set()

    # ------------------------------------------------------------------ #
    # scheduling
    # ------------------------------------------------------------------ #
    def _heartbeat(self, open_issues: int) -> None:
        tasks = self.store.all_tasks()
        done = sum(1 for t in tasks if t.state == State.READY_FOR_HUMAN)
        parked = sum(1 for t in tasks if t.state == State.PARKED)
        spend = sum(t.cost_usd for t in tasks)
        working = ", ".join(f"#{n}" for n in sorted(self._running)) or "idle"
        log.info("%d open issues · %d/%d agents busy (%s) · %d done · %d parked · $%.2f spent",
                 open_issues, len(self._running), self.cfg.loop.max_concurrent_agents,
                 working, done, parked, spend)

    async def _poll_issues(self) -> None:
        issues = await asyncio.to_thread(trackable_issues, self.gh, self.cfg, self.store)
        self._heartbeat(len(issues))
        for issue in issues:
            if self.shutdown.is_set():
                return
            number = issue["number"]
            if number in self._running:
                continue
            task = self.store.get(number)
            if task and task.is_terminal:
                continue
            if self.store.open_pr_count() >= self.cfg.loop.max_open_prs:
                log.info("max_open_prs=%d reached — holding off", self.cfg.loop.max_open_prs)
                return
            if task is None:
                self.store.create(number, issue.get("title", ""))
                log.info("queued #%s: %s", number, issue.get("title", "")[:70])
            self._spawn(number)

    def _spawn(self, issue_number: int) -> None:
        if issue_number in self._running:
            return
        task = asyncio.create_task(self._guarded(issue_number), name=f"issue-{issue_number}")
        self._running[issue_number] = task
        task.add_done_callback(lambda _t: self._running.pop(issue_number, None))

    async def _guarded(self, issue_number: int) -> None:
        async with self._sem:
            if self.shutdown.is_set():
                return
            try:
                await self.drive(issue_number)
            except asyncio.CancelledError:
                self.store.event(issue_number, "cancelled by shutdown")
                raise
            except Exception as exc:  # noqa: BLE001 - daemon must survive one bad task
                log.exception("#%s failed", issue_number)
                self._park(issue_number, f"unhandled error: {exc}")

    # ------------------------------------------------------------------ #
    # the state machine
    # ------------------------------------------------------------------ #
    async def drive(self, issue_number: int) -> Task | None:
        task = self.store.get(issue_number) or self.store.create(issue_number, "")
        if task.is_terminal:
            log.info("#%s is %s — nothing to do", issue_number, task.state)
            return task

        lock_path = self.cfg.var_dir / "locks" / f"issue-{issue_number}.lock"
        try:
            with file_lock(lock_path):
                return await self._drive_locked(issue_number)
        except GitError as exc:
            log.warning("#%s: %s", issue_number, exc)
            return self.store.get(issue_number)

    async def _drive_locked(self, issue_number: int) -> Task | None:
        issue = await asyncio.to_thread(self.gh.issue, issue_number)
        task = self.store.update(issue_number, title=issue.get("title", ""))

        try:
            task = await self._claim(task, issue)
            task = await self._adopt_existing_pr(task)
            path = await asyncio.to_thread(self.wt.ensure_worktree, issue_number)
            self.store.set_state(issue_number, State.WORKTREE, f"worktree at {path}",
                                 branch=self.wt.branch_for(issue_number))

            if task.pr_number is None:
                task = await self._solve(task, issue, path)
                task = await self._push_and_open_pr(task, issue, path)
            return await self._review_loop(task, path)
        except Parked as exc:
            return self._park(issue_number, str(exc))
        except (GHError, GitError) as exc:
            return self._park(issue_number, f"{type(exc).__name__}: {exc}")

    # --- phases ----------------------------------------------------------
    async def _claim(self, task: Task, issue: dict[str, Any]) -> Task:
        if task.state != State.PENDING:
            return task
        body = (f"{MARKER}\n🤖 **Looper** picked this up. An agent is working on it in "
                f"`{self.wt.branch_for(task.issue_number)}`; a PR will follow and will be revised "
                f"until Greptile scores it {self.cfg.greptile.target_score}/5.")
        await asyncio.to_thread(self.gh.add_label, task.issue_number, LABEL_IN_PROGRESS)
        await asyncio.to_thread(self.gh.comment, task.issue_number, body)
        return self.store.set_state(task.issue_number, State.CLAIMED, "claimed issue")

    async def _adopt_existing_pr(self, task: Task) -> Task:
        """A PR may already exist (restart with an empty DB, or a re-run)."""
        if task.pr_number:
            return task
        branch = self.wt.branch_for(task.issue_number)
        existing = await asyncio.to_thread(self.gh.find_pr_for_branch, branch)
        if not existing or existing.get("state") != "OPEN":
            return task
        log.info("#%s: adopting existing PR #%s", task.issue_number, existing["number"])
        return self.store.set_state(
            task.issue_number, State.PR_OPEN, f"adopted PR #{existing['number']}",
            pr_number=existing["number"], head_sha=existing.get("headRefOid"),
            pushed_at=task.pushed_at or now(),
        )

    async def _solve(self, task: Task, issue: dict[str, Any], path: Path) -> Task:
        # Re-entering SOLVING after a crash: the recorded session already exists, so it
        # must be resumed. Passing --session-id again fails with "already in use".
        session_id = task.session_id or str(uuid.uuid4())
        resume = bool(task.session_id)
        self.store.set_state(task.issue_number, State.SOLVING, "agent solving issue",
                             session_id=session_id)
        prompt = render(
            "solve_issue.md",
            REPO=self.cfg.repo.slug,
            WORKTREE=path,
            BRANCH=self.wt.branch_for(task.issue_number),
            BASE_BRANCH=self.cfg.repo.base_branch,
            ISSUE_NUMBER=task.issue_number,
            ISSUE_TITLE=issue.get("title", ""),
            ISSUE_BODY=issue.get("body") or "(no description)",
            ISSUE_COMMENTS=self._format_issue_comments(issue),
            TARGET_SCORE=self.cfg.greptile.target_score,
        )
        before = await asyncio.to_thread(self.wt.head_sha, path)
        result = await self._run_agent(task, "solve", prompt, path, session_id, resume=resume)
        if not result.ok and self._is_session_error(result.error):
            log.warning("#%s: session %s unusable (%s) — starting a fresh one",
                        task.issue_number, session_id, result.error)
            session_id = str(uuid.uuid4())
            self.store.update(task.issue_number, session_id=session_id)
            result = await self._run_agent(task, "solve", prompt, path, session_id, resume=False)

        if ABORT_TOKEN in result.result_text:
            reason = result.result_text.split(ABORT_TOKEN, 1)[1].strip().splitlines()[0]
            raise Parked(f"agent declined the issue — {reason}")
        await asyncio.to_thread(self.wt.commit_all, path,
                                f"chore: agent work for #{task.issue_number}")
        if await asyncio.to_thread(self.wt.commits_ahead, path) == 0:
            raise Parked("agent produced no commits" +
                         (f" (run failed: {result.error})" if result.error else ""))
        after = await asyncio.to_thread(self.wt.head_sha, path)
        if not result.ok and after == before:
            # commits in the worktree are leftovers from an earlier attempt, not this run
            raise Parked(f"agent run failed and added nothing: {result.error}")
        if not result.ok:
            log.warning("#%s: agent run ended with %s but left commits — continuing",
                        task.issue_number, result.error)
        return self.store.get(task.issue_number)  # type: ignore[return-value]

    @staticmethod
    def _is_session_error(error: str | None) -> bool:
        low = (error or "").lower()
        return "session id" in low or "no conversation found" in low

    @staticmethod
    def _extract_pr_body(result_text: str) -> str | None:
        """Agents describe their own PR — some repos' review rules live in the description."""
        m = PR_BODY_RE.search(result_text or "")
        return m.group(1).strip() or None if m else None

    def _compose_pr_body(self, issue_number: int, agent_body: str | None, stat: str) -> str:
        parts = [f"Closes #{issue_number}", ""]
        if agent_body:
            parts += [agent_body, ""]
        parts += [MARKER,
                  f"<sub>Opened by Looper; an agent revises this branch until Greptile "
                  f"scores it {self.cfg.greptile.target_score}/5.</sub>",
                  "", f"<details><summary>diffstat</summary>\n\n```\n{stat}\n```\n\n</details>"]
        return "\n".join(parts)

    async def _push_and_open_pr(self, task: Task, issue: dict[str, Any], path: Path) -> Task:
        branch = self.wt.branch_for(task.issue_number)
        head = await asyncio.to_thread(self.wt.push, path, branch)
        task = self.store.set_state(task.issue_number, State.PUSHED, f"pushed {head[:7]}",
                                    head_sha=head, pushed_at=now())

        stat = await asyncio.to_thread(self.wt.diff_stat, path)
        body = self._compose_pr_body(task.issue_number,
                                     self._pending_pr_body.pop(task.issue_number, None), stat)
        existing = await asyncio.to_thread(self.gh.find_pr_for_branch, branch)
        if existing and existing.get("state") == "OPEN":
            pr_number = existing["number"]
        else:
            pr_number = await asyncio.to_thread(
                self.gh.create_pr, head=branch, base=self.cfg.repo.base_branch,
                title=f"{issue.get('title', f'Fix issue #{task.issue_number}')} (#{task.issue_number})",
                body=body,
            )
        if pr_number is None:  # dry run
            raise Parked("dry-run: stopped before opening a PR")
        return self.store.set_state(task.issue_number, State.PR_OPEN, f"opened PR #{pr_number}",
                                    pr_number=pr_number)

    async def _review_loop(self, task: Task, path: Path) -> Task:
        cfg = self.cfg
        while True:
            task = self.store.get(task.issue_number)  # type: ignore[assignment]
            if task.pr_number is None:
                raise Parked("no PR to review")
            self.store.set_state(task.issue_number, State.AWAITING_REVIEW,
                                 f"waiting for Greptile on {(task.head_sha or '')[:7]}")
            log.info("#%s: PR #%s pushed at %s — waiting for Greptile (poll %ss, timeout %sm)",
                     task.issue_number, task.pr_number, (task.head_sha or "")[:7],
                     self.cfg.greptile.poll_interval_sec, self.cfg.greptile.review_timeout_sec // 60)
            review = await self._get_review(task)
            if review is None or review.score is None:
                raise Parked("Greptile never returned a score for this head")

            task = self.store.record_score(task.issue_number, review.score)
            self.store.set_state(task.issue_number, State.SCORED,
                                 f"score {review.score}/5 (round {task.iteration + 1})")
            self._write_review_log(task, review)

            if review.score >= cfg.greptile.target_score:
                return self._succeed(task, review)

            task = self.store.update(task.issue_number, iteration=task.iteration + 1)
            if task.iteration >= cfg.loop.max_iterations:
                raise Parked(f"hit max_iterations={cfg.loop.max_iterations}; "
                             f"best score {task.best_score}/5")
            if task.cost_usd >= cfg.safety.max_cost_per_issue_usd:
                raise Parked(f"budget exhausted (${task.cost_usd:.2f}); best score {task.best_score}/5")

            await self._revise(task, review, path)
            task = self.store.get(task.issue_number)  # type: ignore[assignment]

    async def _get_review(self, task: Task) -> ReviewResult | None:
        fakes = self.cfg.greptile.fake_scores
        if fakes:
            score = fakes[min(task.iteration, len(fakes) - 1)]
            log.info("#%s: injecting fake score %s/5", task.issue_number, score)
            await asyncio.sleep(0)
            return greptile.fake_review(score)

        def bump(count: int) -> None:
            self.store.update(task.issue_number, retriggers=count)

        return await greptile.wait_for_review(
            self.gh, self.cfg.greptile, task.pr_number,  # type: ignore[arg-type]
            head_sha=task.head_sha, pushed_at=task.pushed_at,
            retriggers_used=task.retriggers, on_retrigger=bump,
        )

    async def _revise(self, task: Task, review: ReviewResult, path: Path) -> Task:
        self.store.set_state(task.issue_number, State.REVISING,
                             f"revising after {review.score}/5 (round {task.iteration})")
        prompt = render(
            "address_review.md",
            REPO=self.cfg.repo.slug,
            WORKTREE=path,
            BRANCH=self.wt.branch_for(task.issue_number),
            BASE_BRANCH=self.cfg.repo.base_branch,
            ISSUE_NUMBER=task.issue_number,
            PR_NUMBER=task.pr_number,
            SCORE=review.score,
            TARGET_SCORE=self.cfg.greptile.target_score,
            ITERATION=task.iteration,
            MAX_ITERATIONS=self.cfg.loop.max_iterations,
            REVIEW=review.render(),
        )
        before = await asyncio.to_thread(self.wt.head_sha, path)
        result = await self._run_agent(task, f"revise-{task.iteration}", prompt, path,
                                       task.session_id, resume=bool(task.session_id))
        await asyncio.to_thread(self.wt.commit_all, path,
                                f"fix: address review feedback (#{task.issue_number})")
        after = await asyncio.to_thread(self.wt.head_sha, path)
        if after == before:
            raise Parked("agent made no changes in the revision round" +
                         (f" ({result.error})" if result.error else ""))

        branch = self.wt.branch_for(task.issue_number)
        head = await asyncio.to_thread(self.wt.push, path, branch)
        agent_body = self._pending_pr_body.pop(task.issue_number, None)
        if agent_body and task.pr_number:
            stat = await asyncio.to_thread(self.wt.diff_stat, path)
            await asyncio.to_thread(self.gh.edit_pr_body, task.pr_number,
                                    self._compose_pr_body(task.issue_number, agent_body, stat))
            self.store.event(task.issue_number, "updated PR description from agent")
        return self.store.set_state(task.issue_number, State.PUSHED,
                                    f"pushed revision {head[:7]}",
                                    head_sha=head, pushed_at=now())

    # --- terminal states -------------------------------------------------
    def _succeed(self, task: Task, review: ReviewResult) -> Task:
        n, pr = task.issue_number, task.pr_number
        rounds = task.iteration + 1
        body = (f"{MARKER}\n✅ Greptile scored this **{review.score}/5** after {rounds} review "
                f"round(s). Total agent spend: ${task.cost_usd:.2f}. Ready for a human to merge.")
        try:
            if pr:
                self.gh.add_pr_label(pr, LABEL_READY)
                self.gh.pr_comment(pr, body)
            self.gh.remove_label(n, LABEL_IN_PROGRESS)
        except GHError as exc:
            log.warning("#%s: could not finalise labels/comments: %s", n, exc)
        log.info("#%s DONE — PR #%s scored %s/5 in %d round(s), $%.2f",
                 n, pr, review.score, rounds, task.cost_usd)
        return self.store.set_state(n, State.READY_FOR_HUMAN,
                                    f"{review.score}/5 after {rounds} round(s)", error=None)

    def _park(self, issue_number: int, reason: str) -> Task:
        task = self.store.get(issue_number)
        pr = task.pr_number if task else None
        log.warning("#%s PARKED: %s", issue_number, reason)
        body = (f"{MARKER}\n🅿️ **Looper parked this task** — a human needs to take over.\n\n"
                f"**Reason:** {reason}\n"
                f"**Best Greptile score:** {task.best_score if task else '—'}/5\n"
                f"**Review rounds:** {task.iteration if task else 0}\n"
                f"**Agent spend:** ${task.cost_usd if task else 0:.2f}\n\n"
                f"The branch and worktree are left intact for inspection.")
        try:
            self.gh.add_label(issue_number, LABEL_NEEDS_HUMAN)
            self.gh.remove_label(issue_number, LABEL_IN_PROGRESS)
            if pr:
                self.gh.add_pr_label(pr, LABEL_NEEDS_HUMAN)
                self.gh.pr_comment(pr, body)
            else:
                self.gh.comment(issue_number, body)
        except GHError as exc:
            log.warning("#%s: could not post park notice: %s", issue_number, exc)
        return self.store.set_state(issue_number, State.PARKED, f"parked: {reason}", error=reason)

    # --- helpers ---------------------------------------------------------
    async def _run_agent(self, task: Task, kind: str, prompt: str, path: Path,
                         session_id: str | None, *, resume: bool) -> RunResult:
        log_dir = self.cfg.logs_dir / f"issue-{task.issue_number}"
        log_path = log_dir / f"{task.iteration:02d}-{kind}.jsonl"
        (log_dir / f"{task.iteration:02d}-{kind}.prompt.md").parent.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{task.iteration:02d}-{kind}.prompt.md").write_text(prompt)

        attempt = self.store.start_attempt(task.issue_number, task.iteration, kind, session_id)
        result = await self.agent.run(prompt, cwd=path, log_path=log_path,
                                      session_id=session_id, resume=resume)
        self.store.finish_attempt(
            attempt, ok=result.ok, num_turns=result.num_turns, cost_usd=result.cost_usd,
            result_text=result.result_text[:4000], log_path=str(log_path),
            session_id=result.session_id,
        )
        if body := self._extract_pr_body(result.result_text):
            self._pending_pr_body[task.issue_number] = body
        total = self.store.add_cost(task.issue_number, result.cost_usd)
        if result.session_id and result.session_id != task.session_id:
            self.store.update(task.issue_number, session_id=result.session_id)
        self.store.event(task.issue_number, f"agent {kind}: {result.summary}",
                         data={"cost_total": total, "log": str(log_path)})
        log.info("#%s agent %s: %s (total $%.2f)", task.issue_number, kind, result.summary, total)
        return result

    def _write_review_log(self, task: Task, review: ReviewResult) -> None:
        log_dir = self.cfg.logs_dir / f"issue-{task.issue_number}"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{task.iteration:02d}-review.md").write_text(review.render())
        self.store.event(task.issue_number, f"greptile {review.score}/5",
                         data={"source": review.source, "url": review.url,
                               "inline": len(review.inline)})

    @staticmethod
    def _format_issue_comments(issue: dict[str, Any], limit: int = 8) -> str:
        comments = [c for c in issue.get("comments", []) if MARKER not in (c.get("body") or "")]
        if not comments:
            return ""
        out = ["# Discussion on the issue"]
        for c in comments[-limit:]:
            author = (c.get("author") or {}).get("login", "someone")
            out.append(f"**@{author}:**\n{(c.get('body') or '').strip()[:2000]}")
        return "\n\n".join(out)
