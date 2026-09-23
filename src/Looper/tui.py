"""Curses status viewer over the same SQLite state the CLI reads.

Embeds the daemon loop in a background thread so `looper tui` alone drives
agents — no separate `looper daemon` process needed. Store is safe to share:
it already uses check_same_thread=False plus an RLock (see state.py).
Press `D` to stop or restart that thread without leaving the TUI, and `d` to
page through the selected task's agent transcripts.

The daemon's `logging` output would otherwise land on stdout and corrupt the
curses screen, so it's captured into an in-memory ring buffer instead and
rendered as a scrollable panel at the bottom of the screen.
"""

from __future__ import annotations

import asyncio
import collections
import curses
import json
import logging
import os
import shlex
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .agent import tool_summary
from .config import Config
from .gh import GHError
from .orchestrator import LABEL_NEEDS_HUMAN, Orchestrator
from .state import State, Store, Task
from .worktree import Worktrees

log = logging.getLogger("looper.tui")

REFRESH_MS = 2000
LOG_MAXLINES = 2000
LOG_MIN_HEIGHT = 5
HELP = ("j/k move  a start  r retry  e requeue  x abandon  c clean  d transcript  D daemon  "
        "PgUp/PgDn log  q quit")
HEADER = (f"{'ISSUE':>6}  {'STATE':<16} {'PR':>5}  {'IT':>2} {'SCORE':>5} {'BEST':>4} "
          f"{'COST':>7} {'AGO':>4}  TITLE")

# what a glance at the table should tell you: who needs a human, who is busy
GROUPS = {
    "working": {State.CLAIMED, State.WORKTREE, State.SOLVING, State.REVISING},
    "review": {State.PUSHED, State.PR_OPEN, State.AWAITING_REVIEW, State.SCORED},
    "ready": {State.READY_FOR_HUMAN},
    "parked": {State.PARKED},
}
COLORS = {"working": curses.COLOR_CYAN, "review": curses.COLOR_YELLOW,
          "ready": curses.COLOR_GREEN, "parked": curses.COLOR_RED}


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


def group(state: str) -> str | None:
    return next((g for g, states in GROUPS.items() if state in states), None)


def summary(tasks: list[Task]) -> str:
    """'2 working · 1 ready' — empty groups left out."""
    counts = collections.Counter(group(t.state) for t in tasks)
    return " · ".join(f"{counts[g]} {g}" for g in GROUPS if counts[g])


def ago(ts: str, now: datetime | None = None) -> str:
    """ISO timestamp -> '45s' / '12m' / '3h' / '2d'."""
    try:
        secs = int(((now or datetime.now(timezone.utc)) - datetime.fromisoformat(ts))
                   .total_seconds())
    except ValueError:
        return "-"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{max(0, secs)}s"


def _attr(name: str | None) -> int:
    """Colour pair for a group or log level; plain on terminals without colour."""
    if name is None or not curses.has_colors():
        return 0
    return curses.color_pair(list(COLORS).index(name) + 1)


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    for i, color in enumerate(COLORS.values(), start=1):
        curses.init_pair(i, color, -1)


def act(key: int, task: Task, store: Store, cfg: Config,
        orch: Orchestrator | None, loop: asyncio.AbstractEventLoop | None) -> str:
    """Apply a/r/e/x/c to one task. a/r resume the daemon's own spawn machinery
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
    if key == ord("e"):
        if task.state != State.PARKED:
            return f"#{task.issue_number} not parked — not requeued"
        # labels first: if GitHub refuses, the task stays PARKED so `e` can be retried
        if orch is not None:
            try:
                orch.gh.remove_label(task.issue_number, LABEL_NEEDS_HUMAN)
                if task.pr_number:
                    orch.gh.remove_pr_label(task.pr_number, LABEL_NEEDS_HUMAN)
            except GHError as exc:
                return f"#{task.issue_number} still parked — could not drop label: {exc}"
        state = State.PR_OPEN if task.pr_number else State.PENDING
        store.set_state(task.issue_number, state, "requeued from tui", error=None)
        return f"#{task.issue_number} requeued -> {state}"
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


def _run_order(path: Path) -> tuple[int, str]:
    """'100-revise-100' after '99-revise-99': the iteration prefix grows past 2 digits."""
    iteration, _, kind = path.stem.partition("-")
    return (int(iteration) if iteration.isdigit() else -1, kind)


def transcript(log_dir: Path) -> str:
    """Every stream-json run in `log_dir`, oldest first, as readable text:
    what the agent said, which tools it called, how each run ended."""
    out: list[str] = []
    for path in sorted(log_dir.glob("*.jsonl"), key=_run_order):
        out.append(f"══ {path.stem} ".ljust(72, "═"))
        for raw in path.read_text(errors="replace").splitlines():
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "assistant":
                for block in msg.get("message", {}).get("content", []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        out.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        out.append(f"→ {tool_summary(block)}")
            elif msg.get("type") == "result":
                out.append(f"── {msg.get('subtype')}: {msg.get('num_turns', 0)} turns, "
                           f"${msg.get('total_cost_usd') or 0.0:.2f}")
                if msg.get("result"):
                    out.append(msg["result"])
            elif msg.get("dry_run"):
                out.append("[dry-run] no agent ran")
        out.append("")
    return "\n".join(out)


def _page(win, text: str) -> str:
    """Hand the screen to $PAGER (default less) and take it back when it quits."""
    pager = os.environ.get("PAGER", "").strip() or "less"
    curses.endwin()
    try:
        done = subprocess.run(shlex.split(pager), input=text.encode(), check=False)
    except (OSError, ValueError) as exc:  # missing binary / unbalanced quotes in $PAGER
        return f"pager {pager!r} failed: {exc}"
    finally:
        win.refresh()
    return f"pager {pager!r} exited {done.returncode}" if done.returncode else ""


def _row(t: Task, width: int) -> str:
    score = f"{t.last_score}/5" if t.last_score is not None else "-"
    best = str(t.best_score) if t.best_score is not None else "-"
    title = t.title[: max(0, width - 60)]
    return (f"{t.issue_number:>6}  {t.state:<16} {t.pr_number or '-':>5}  {t.iteration:>2} "
            f"{score:>5} {best:>4} {'$%.2f' % t.cost_usd:>7} {ago(t.updated_at):>4}  {title}")


def _detail(t: Task) -> str:
    line = f"#{t.issue_number} {t.state} {ago(t.updated_at)} ago"
    return f"{line} — {t.error}" if t.error else line


def _draw(win, cfg: Config, tasks: list[Task], selected: int, status: str,
          log_lines: list[str], log_scroll: int, daemon_on: bool) -> None:
    win.erase()
    h, w = win.getmaxyx()
    log_h = max(LOG_MIN_HEIGHT, h // 3)
    table_bottom = max(3, h - log_h - 3)  # -3: detail line, log divider, footer

    daemon = "● on" if daemon_on else "○ OFF"
    bar = f" looper  {cfg.repo.slug}   daemon {daemon}   {summary(tasks)}"
    win.addnstr(0, 0, bar.ljust(w - 1), w - 1, curses.A_REVERSE | curses.A_BOLD)
    win.addnstr(1, 0, HEADER, w - 1, curses.A_UNDERLINE)
    if not tasks:
        win.addnstr(2, 2, 'no tasks — label an issue "agent" to queue it', w - 3, curses.A_DIM)
    for i, t in enumerate(tasks):
        if 2 + i >= table_bottom:
            break
        attr = _attr(group(t.state)) | (curses.A_REVERSE if i == selected else 0)
        win.addnstr(2 + i, 0, _row(t, w).ljust(w - 1), w - 1, attr)

    if status or tasks:
        win.addnstr(table_bottom, 0, status or _detail(tasks[selected]), w - 1, curses.A_BOLD)
    following = "live" if log_scroll == 0 else f"scrolled back {log_scroll}"
    win.addnstr(table_bottom + 1, 0, f"── log ({following}) ".ljust(w - 1, "─"), w - 1, curses.A_DIM)
    log_top = table_bottom + 2
    for i, line in enumerate(visible_log(log_lines, h - 1 - log_top, log_scroll)):
        level = "parked" if " ERROR " in line else "review" if " WARNING " in line else None
        win.addnstr(log_top + i, 0, line, w - 1, _attr(level))
    win.addnstr(h - 1, 0, HELP, w - 1, curses.A_DIM)
    win.refresh()


def _loop(win, cfg: Config, store: Store, orch: Orchestrator,
          loop: asyncio.AbstractEventLoop, log_buf: collections.deque[str],
          daemon_thread: threading.Thread) -> threading.Thread:
    curses.curs_set(0)
    _init_colors()
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
        elif key == ord("d") and tasks:
            issue = tasks[selected].issue_number
            text = transcript(cfg.logs_dir / f"issue-{issue}")
            status = _page(win, text) if text else f"#{issue} no transcript yet"
        elif key == ord("D"):
            if daemon_thread.is_alive():
                loop.call_soon_threadsafe(orch.shutdown.set)
                status = "daemon: stopping"
            else:
                daemon_thread = _start_daemon(loop, orch)
                status = "daemon: starting"
        elif key in (ord("a"), ord("r"), ord("e"), ord("x"), ord("c")) and tasks:
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
