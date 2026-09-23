"""Curses status viewer over the same SQLite state the CLI reads.

Embeds the daemon loop in a background thread so `looper tui` alone drives
agents — no separate `looper daemon` process needed. Store is safe to share:
it already uses check_same_thread=False plus an RLock (see state.py).
Press `d` to stop or restart that thread without leaving the TUI.

The daemon's `logging` output would otherwise land on stdout and corrupt the
curses screen, so it's captured into an in-memory ring buffer instead and
rendered as a scrollable panel at the bottom of the screen.
"""

from __future__ import annotations

import asyncio
import collections
import curses
import logging
import threading

from .config import Config
from .orchestrator import Orchestrator
from .state import State, Store, Task
from .worktree import Worktrees

log = logging.getLogger("looper.tui")

REFRESH_MS = 2000
LOG_MAXLINES = 2000
LOG_MIN_HEIGHT = 5
HELP = ("[j/k] move  [a] start agent  [r] retry  [x] abandon  [c] clean worktree  "
        "[d] daemon on/off  [PgUp/PgDn] scroll log  [q] quit")
HEADER = f"{'ISSUE':>6}  {'STATE':<16} {'PR':>5}  {'IT':>2} {'SCORE':>5} {'BEST':>4} {'COST':>7}  TITLE"


class _BufferHandler(logging.Handler):
    """Appends formatted log lines to a bounded deque instead of a stream."""

    def __init__(self, buf: collections.deque[str]):
        super().__init__()
        self.buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        self.buf.append(self.format(record))


def _capture_logs() -> collections.deque[str]:
    buf: collections.deque[str] = collections.deque(maxlen=LOG_MAXLINES)
    handler = _BufferHandler(buf)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-16s %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()  # a stdout handler would corrupt the curses screen
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return buf


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


def scroll_log(key: int, total: int, height: int, scroll: int) -> int:
    """Lines back from the live tail. 0 = following; clamped to what's buffered."""
    top = max(0, total - height)
    if key == curses.KEY_PPAGE:
        scroll += height
    elif key == curses.KEY_NPAGE:
        scroll -= height
    return max(0, min(top, scroll))


def visible_log(lines: list[str], height: int, scroll: int) -> list[str]:
    """The `height` lines that should be on screen, `scroll` lines back from live."""
    if height <= 0:
        return []
    end = len(lines) - scroll
    start = max(0, end - height)
    return lines[start:end]


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


def _draw(win, cfg: Config, tasks: list[Task], selected: int, status: str,
          log_lines: list[str], log_scroll: int, daemon_on: bool) -> None:
    win.erase()
    h, w = win.getmaxyx()
    log_h = max(LOG_MIN_HEIGHT, h // 3)
    table_bottom = max(4, h - log_h - 2)  # -2: divider + log title row

    daemon_label = "on" if daemon_on else "OFF"
    win.addnstr(0, 0, f"looper — {cfg.repo.slug}  ({len(tasks)} tasks)  daemon:{daemon_label}",
                w - 1, curses.A_BOLD)
    win.addnstr(1, 0, HELP, w - 1)
    win.addnstr(2, 0, status, w - 1)
    win.addnstr(3, 0, HEADER, w - 1, curses.A_UNDERLINE)
    for i, t in enumerate(tasks):
        if 4 + i >= table_bottom:
            break
        win.addnstr(4 + i, 0, _row(t, w), w - 1, curses.A_REVERSE if i == selected else 0)

    win.addnstr(table_bottom, 0, "-" * (w - 1), w - 1)
    following = " (live)" if log_scroll == 0 else f" (scrolled back {log_scroll})"
    win.addnstr(table_bottom + 1, 0, f"daemon log{following}", w - 1, curses.A_BOLD)
    log_area_h = h - (table_bottom + 2)
    for i, line in enumerate(visible_log(log_lines, log_area_h, log_scroll)):
        win.addnstr(table_bottom + 2 + i, 0, line, w - 1)
    win.refresh()


def _loop(win, cfg: Config, store: Store, orch: Orchestrator,
          loop: asyncio.AbstractEventLoop, log_buf: collections.deque[str],
          daemon_thread: threading.Thread) -> threading.Thread:
    curses.curs_set(0)
    win.timeout(REFRESH_MS)
    selected = 0
    log_scroll = 0
    status = ""
    while True:
        # merged work is done work — it stays in the db and in `looper status`,
        # but the tui is a worklist, not a history
        tasks = [t for t in store.all_tasks() if t.state != State.MERGED]
        selected = navigate(-1, len(tasks), selected)  # clamp after tasks may have shrunk
        lines = list(log_buf)
        h, _w = win.getmaxyx()
        log_h = max(LOG_MIN_HEIGHT, h // 3)
        log_scroll = scroll_log(-1, len(lines), log_h, log_scroll)  # reclamp as lines arrive
        _draw(win, cfg, tasks, selected, status, lines, log_scroll, daemon_thread.is_alive())
        status = ""

        key = win.getch()
        if key in (ord("q"), 27):
            return daemon_thread
        if key in (curses.KEY_UP, curses.KEY_DOWN, ord("j"), ord("k")):
            selected = navigate(key, len(tasks), selected)
        elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            log_scroll = scroll_log(key, len(lines), log_h, log_scroll)
        elif key == ord("d"):
            if daemon_thread.is_alive():
                loop.call_soon_threadsafe(orch.shutdown.set)
                status = "daemon: stopping"
            else:
                daemon_thread = _start_daemon(loop, orch)
                status = "daemon: starting"
        elif key in (ord("a"), ord("r"), ord("x"), ord("c")) and tasks:
            status = act(key, tasks[selected], store, cfg, orch, loop)


def _run_daemon_thread(loop: asyncio.AbstractEventLoop, orch: Orchestrator) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(orch.run_daemon(install_signals=False))
    except Exception:
        log.exception("embedded daemon loop crashed")


def _start_daemon(loop: asyncio.AbstractEventLoop, orch: Orchestrator) -> threading.Thread:
    """(Re)start the embedded daemon. Reuses `loop` — it's only closed when the TUI exits."""
    orch.shutdown.clear()
    thread = threading.Thread(target=_run_daemon_thread, args=(loop, orch), daemon=True)
    thread.start()
    return thread


def run(cfg: Config) -> int:
    from .cli import build  # local: cli imports tui lazily too, avoid a module-level cycle
    log_buf = _capture_logs()
    store, _gh, _wt, orch = build(cfg)
    loop = asyncio.new_event_loop()
    thread = _start_daemon(loop, orch)
    try:
        thread = curses.wrapper(_loop, cfg, store, orch, loop, log_buf, thread)
    finally:
        loop.call_soon_threadsafe(orch.shutdown.set)
        thread.join(timeout=15)
        loop.close()
        store.close()
    return 0
