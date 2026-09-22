"""Curses status viewer over the same SQLite state the CLI reads.

Embeds the daemon loop in a background thread so `looper tui` alone drives
agents — no separate `looper daemon` process needed. Store is safe to share:
it already uses check_same_thread=False plus an RLock (see state.py).
"""

from __future__ import annotations

import asyncio
import curses
import logging
import threading

from .config import Config
from .orchestrator import Orchestrator
from .state import State, Store, Task
from .worktree import Worktrees

log = logging.getLogger("looper.tui")

REFRESH_MS = 2000
HELP = "[j/k] move  [a] start agent  [r] retry  [x] abandon  [c] clean worktree  [q] quit"
HEADER = f"{'ISSUE':>6}  {'STATE':<16} {'PR':>5}  {'IT':>2} {'SCORE':>5} {'BEST':>4} {'COST':>7}  TITLE"


def _worktrees(cfg: Config) -> Worktrees:
    return Worktrees(
        cfg.repo.slug, base_branch=cfg.repo.base_branch, repos_dir=cfg.repos_dir,
        worktrees_dir=cfg.worktrees_dir, branch_prefix=cfg.safety.branch_prefix,
        dry_run=cfg.safety.dry_run,
    )


def navigate(key: int, count: int, selected: int) -> int:
    """Pure cursor math — kept separate from curses I/O so it's testable."""
    if count == 0:
        return 0
    if key in (curses.KEY_UP, ord("k")):
        return max(0, selected - 1)
    if key in (curses.KEY_DOWN, ord("j")):
        return min(count - 1, selected + 1)
    return min(selected, count - 1)


def act(key: int, task: Task, store: Store, cfg: Config,
        orch: Orchestrator | None, loop: asyncio.AbstractEventLoop | None) -> str:
    """Apply a/r/x/c to one task. a/r resume the daemon's own spawn machinery
    (loop.call_soon_threadsafe -> Orchestrator._spawn) instead of duplicating it."""
    if key == ord("a"):
        if task.is_terminal:
            state = State.PR_OPEN if task.pr_number else State.PENDING
            store.set_state(task.issue_number, state, "started from tui", error=None)
        if orch is not None and loop is not None:
            loop.call_soon_threadsafe(orch._spawn, task.issue_number)
        return f"#{task.issue_number} solving"
    if key == ord("r"):
        state = State.PR_OPEN if task.pr_number else State.PENDING
        store.set_state(task.issue_number, state, "retried from tui", error=None)
        return f"#{task.issue_number} -> {state}"
    if key == ord("x"):
        store.set_state(task.issue_number, State.PARKED, "abandoned from tui",
                         error="abandoned by operator")
        return f"#{task.issue_number} parked"
    if key == ord("c"):
        if not task.is_terminal:
            return f"#{task.issue_number} still active — not cleaned"
        _worktrees(cfg).remove(task.issue_number)
        return f"#{task.issue_number} worktree removed"
    return ""


def _row(t: Task, width: int) -> str:
    score = f"{t.last_score}/5" if t.last_score is not None else "-"
    best = str(t.best_score) if t.best_score is not None else "-"
    title = t.title[: max(0, width - 55)]
    return (f"{t.issue_number:>6}  {t.state:<16} {t.pr_number or '-':>5}  {t.iteration:>2} "
            f"{score:>5} {best:>4} {'$%.2f' % t.cost_usd:>7}  {title}")


def _draw(win, cfg: Config, tasks: list[Task], selected: int, status: str) -> None:
    win.erase()
    h, w = win.getmaxyx()
    win.addnstr(0, 0, f"looper — {cfg.repo.slug}  ({len(tasks)} tasks)", w - 1, curses.A_BOLD)
    win.addnstr(1, 0, HELP, w - 1)
    win.addnstr(3, 0, HEADER, w - 1, curses.A_UNDERLINE)
    for i, t in enumerate(tasks):
        if 4 + i >= h - 1:
            break
        win.addnstr(4 + i, 0, _row(t, w), w - 1, curses.A_REVERSE if i == selected else 0)
    if status:
        win.addnstr(h - 1, 0, status, w - 1)
    win.refresh()


def _loop(win, cfg: Config, store: Store,
          orch: Orchestrator, loop: asyncio.AbstractEventLoop) -> None:
    curses.curs_set(0)
    win.timeout(REFRESH_MS)
    selected = 0
    status = ""
    while True:
        tasks = store.all_tasks()
        selected = navigate(-1, len(tasks), selected)  # clamp after tasks may have shrunk
        _draw(win, cfg, tasks, selected, status)
        status = ""

        key = win.getch()
        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_UP, curses.KEY_DOWN, ord("j"), ord("k")):
            selected = navigate(key, len(tasks), selected)
        elif key in (ord("a"), ord("r"), ord("x"), ord("c")) and tasks:
            status = act(key, tasks[selected], store, cfg, orch, loop)


def _run_daemon_thread(loop: asyncio.AbstractEventLoop, orch: Orchestrator) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(orch.run_daemon(install_signals=False))
    except Exception:
        log.exception("embedded daemon loop crashed")
    finally:
        loop.close()


def run(cfg: Config) -> int:
    from .cli import build  # local: cli imports tui lazily too, avoid a module-level cycle
    store, _gh, _wt, orch = build(cfg)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=_run_daemon_thread, args=(loop, orch), daemon=True)
    thread.start()
    try:
        curses.wrapper(_loop, cfg, store, orch, loop)
    finally:
        loop.call_soon_threadsafe(orch.shutdown.set)
        thread.join(timeout=15)
        store.close()
    return 0
