"""Curses status viewer over the same SQLite state the CLI reads.

Embeds the daemon loop in a background thread so `looper tui` alone drives
agents — no separate `looper daemon` process needed. Store is safe to share:
it already uses check_same_thread=False plus an RLock (see state.py).
Press `D` to stop or restart that thread without leaving the TUI, `d` to
page through the selected task's agent transcripts, and `R` to switch repo.

The daemon's `logging` output would otherwise land on stdout and corrupt the
curses screen, so it's captured into an in-memory ring buffer instead and
rendered as a scrollable panel at the bottom of the screen.

It opens on a repo picker (skipped with `--repo`): a centred window over a
darkened screen listing every [[repos]] entry, plus `+ add repo…`, which runs
`looper init`'s questions and comes back.
"""

from __future__ import annotations

import argparse
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
from .config import Config, ConfigError, load
from .gh import GHError
from .orchestrator import LABEL_NEEDS_HUMAN, Orchestrator
from .state import State, Store, Task
from .worktree import Worktrees

log = logging.getLogger("looper.tui")

REFRESH_MS = 2000
LOG_MAXLINES = 2000
LOG_MIN_HEIGHT = 5
HELP = ("j/k move  a start  r retry  e requeue  x abandon  c clean  d transcript  D daemon  "
        "R repo  PgUp/PgDn log  q quit")
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
BACKDROP_PAIR = len(COLORS) + 1  # colour pair behind the repo picker
ADD_REPO = "+ add repo…"
PICK_HELP = " enter open · a add · q quit "


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
    # grey on true black (256-colour index 16, which themes leave alone): a shade
    # darker than almost any terminal background, so the picker reads as a window
    deep = curses.COLORS >= 256
    curses.init_pair(BACKDROP_PAIR, 244 if deep else curses.COLOR_WHITE,
                     16 if deep else curses.COLOR_BLACK)


def _backdrop_attr() -> int:
    if not curses.has_colors():
        return curses.A_DIM
    return curses.color_pair(BACKDROP_PAIR) | (0 if curses.COLORS >= 256 else curses.A_DIM)


def _blank_backdrop(win, shade: int) -> None:
    """Behind the picker at start, before any repo is open: just the frame."""
    win.erase()
    w = win.getmaxyx()[1]
    win.addnstr(0, 0, " looper", w - 1, shade | curses.A_BOLD)
    win.addnstr(1, 0, HEADER, w - 1, shade)


def pick_repo(win, slugs: list[str], selected: int = 0, backdrop=_blank_backdrop) -> str | None:
    """Centred chooser over a darkened screen: a slug, ADD_REPO, or None to quit.
    `backdrop(win, shade)` draws what the picker covers, in `shade`."""
    items = [*slugs, ADD_REPO]
    selected = min(selected, len(items) - 1)
    win.timeout(-1)  # the backdrop is frozen while the picker is up, so just wait for a key
    while True:
        h, w = win.getmaxyx()
        shade = _backdrop_attr()
        win.bkgd(" ", shade)
        backdrop(win, shade)
        rows = min(len(items), h - 3)  # border, items, blank line, border
        bw = min(w, max(34, max(map(len, items)) + 8, len(PICK_HELP) + 4))
        if rows < 1 or bw < 12:
            win.addnstr(0, 0, "terminal too small", w - 1)
            win.refresh()
        else:
            top = max(0, selected - rows + 1)  # scroll once repos outgrow the screen
            box = curses.newwin(rows + 3, bw, (h - rows - 3) // 2, (w - bw) // 2)
            box.box()
            box.addnstr(0, 2, " looper ", bw - 4, curses.A_BOLD)
            box.addnstr(rows + 2, max(2, bw - len(PICK_HELP) - 2), PICK_HELP, bw - 4, curses.A_DIM)
            for row, item in enumerate(items[top:top + rows]):
                on = top + row == selected
                attr = (_attr("working") | curses.A_BOLD if on
                        else curses.A_DIM if item == ADD_REPO else 0)
                box.addnstr(1 + row, 2, ("> " if on else "  ") + item, bw - 4, attr)
            win.noutrefresh()
            box.noutrefresh()
            curses.doupdate()
        key = win.getch()
        if key in (ord("q"), 27):
            return None
        if key in (10, 13, curses.KEY_ENTER):
            return items[selected]
        if key == ord("a"):
            return ADD_REPO
        selected = navigate(key, len(items), selected)


def _add_repo(win, path: Path) -> None:
    """Run `looper init`'s questions on the plain terminal, then hand the screen back."""
    from .cli import cmd_init  # local: cli imports tui lazily too
    curses.endwin()
    print("\nAdd a repo to Looper (Ctrl-C goes back)\n")
    try:
        try:
            cmd_init(argparse.Namespace(config=str(path), repo=None, model=None, base_branch=None))
        except ConfigError as exc:
            print(f"config error: {exc}")
        input("\nPress Enter to go back to Looper ")
    except (KeyboardInterrupt, EOFError):
        print()
    win.refresh()


def _choose(win, cfg: Config, current: str = "", backdrop=_blank_backdrop) -> Config | None:
    """Picker until a repo is chosen, starting on `current`; after `+ add repo…` it
    comes back with the new repo highlighted. None = the user backed out."""
    slugs = [r.slug for r in cfg.repos]
    selected = slugs.index(current) if current in slugs else 0
    while True:
        choice = pick_repo(win, [r.slug for r in cfg.repos], selected, backdrop)
        if choice is None:
            return None
        if choice != ADD_REPO:
            cfg.select(choice)
            return cfg
        before = len(cfg.repos)
        _add_repo(win, cfg.path)
        cfg = load(cfg.path, root=cfg.root)
        selected = len(cfg.repos) - 1 if len(cfg.repos) > before else len(cfg.repos)


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
          log_lines: list[str], log_scroll: int, daemon_on: bool, shade: int = 0) -> None:
    """The dashboard. With `shade`, every line is drawn in it and nothing is shown yet:
    that's the frozen backdrop the repo picker sits on."""
    win.erase()
    h, w = win.getmaxyx()

    def put(y: int, x: int, text: str, n: int, attr: int) -> None:
        win.addnstr(y, x, text, n, shade or attr)

    log_h = max(LOG_MIN_HEIGHT, h // 3)
    table_bottom = max(3, h - log_h - 3)  # -3: detail line, log divider, footer

    daemon = "● on" if daemon_on else "○ OFF"
    bar = f" looper  {cfg.repo.slug}   daemon {daemon}   {summary(tasks)}"
    put(0, 0, bar.ljust(w - 1), w - 1, curses.A_REVERSE | curses.A_BOLD)
    put(1, 0, HEADER, w - 1, curses.A_UNDERLINE)
    if not tasks:
        put(2, 2, 'no tasks — label an issue "agent" to queue it', w - 3, curses.A_DIM)
    for i, t in enumerate(tasks):
        if 2 + i >= table_bottom:
            break
        attr = _attr(group(t.state)) | (curses.A_REVERSE if i == selected else 0)
        put(2 + i, 0, _row(t, w).ljust(w - 1), w - 1, attr)

    if status or tasks:
        put(table_bottom, 0, status or _detail(tasks[selected]), w - 1, curses.A_BOLD)
    following = "live" if log_scroll == 0 else f"scrolled back {log_scroll}"
    put(table_bottom + 1, 0, f"── log ({following}) ".ljust(w - 1, "─"), w - 1, curses.A_DIM)
    log_top = table_bottom + 2
    for i, line in enumerate(visible_log(log_lines, h - 1 - log_top, log_scroll)):
        level = "parked" if " ERROR " in line else "review" if " WARNING " in line else None
        put(log_top + i, 0, line, w - 1, _attr(level))
    put(h - 1, 0, HELP, w - 1, curses.A_DIM)
    if not shade:
        win.refresh()


def _worklist(store: Store) -> list[Task]:
    # merged work is done work — it stays in the db and in `looper status`,
    # but the tui is a worklist, not a history
    return [t for t in store.all_tasks() if t.state != State.MERGED]


def _loop(win, cfg: Config, store: Store, orch: Orchestrator,
          loop: asyncio.AbstractEventLoop, log_buf: collections.deque[str],
          daemon_thread: threading.Thread) -> tuple[threading.Thread, bool]:
    """Run the dashboard until q (-> False) or R (-> True: the user wants the repo picker)."""
    win.timeout(REFRESH_MS)
    selected = 0
    log_scroll = 0
    status = ""
    while True:
        tasks = _worklist(store)
        selected = navigate(-1, len(tasks), selected)  # clamp after tasks may have shrunk
        lines = list(log_buf)
        h, _w = win.getmaxyx()
        log_h = max(LOG_MIN_HEIGHT, h // 3)
        log_scroll = scroll_log(-1, len(lines), log_h, log_scroll)  # reclamp as lines arrive
        _draw(win, cfg, tasks, selected, status, lines, log_scroll, daemon_thread.is_alive())
        status = ""

        key = win.getch()
        if key in (ord("q"), 27):
            return daemon_thread, False
        if key == ord("R"):
            return daemon_thread, True
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


def run(cfg: Config, pick: bool = True) -> int:
    """Pick a repo (unless --repo already did), then the dashboard with the daemon
    embedded, until the user quits. `R` switches repo: the old repo's daemon takes no
    new issues but lets its running agents finish in the background, and the new
    repo's daemon starts straight away. Its per-issue locks stop a second agent
    starting on an issue that's still running if you switch back early."""
    from .cli import build  # local: cli imports tui lazily too, avoid a module-level cycle
    log_buf = _capture_logs()
    daemons: list[dict] = []  # every repo opened this session, still draining or not

    def session(win) -> None:
        curses.curs_set(0)
        _init_colors()
        chosen = _choose(win, cfg) if pick else cfg
        while chosen is not None:
            win.bkgd(" ", 0)  # the picker's darkened background goes away with it
            store, _gh, _wt, orch = build(chosen)
            loop = asyncio.new_event_loop()
            d = {"store": store, "orch": orch, "loop": loop, "thread": _start_daemon(loop, orch)}
            daemons.append(d)
            while True:
                d["thread"], switch = _loop(win, chosen, store, orch, loop, log_buf, d["thread"])
                if not switch:
                    return

                def dashboard(win, shade, cfg=chosen, d=d) -> None:
                    _draw(win, cfg, _worklist(d["store"]), 0, "", list(log_buf), 0,
                          d["thread"].is_alive(), shade=shade)

                # fresh from disk: repos may have been added, and select() changed this one
                nxt = _choose(win, load(chosen.path, root=chosen.root), chosen.repo.slug, dashboard)
                win.bkgd(" ", 0)
                if nxt is not None and nxt.repo.slug != chosen.repo.slug:
                    break
                # backed out, or picked the repo that's already open: same dashboard
            log.info("switching to %s: %s takes no new issues, running agents finish",
                     nxt.repo.slug, chosen.repo.slug)
            loop.call_soon_threadsafe(orch.shutdown.set)
            chosen = nxt

    try:
        curses.wrapper(session)
    finally:  # stop every daemon after curses has handed the terminal back
        for d in daemons:
            d["loop"].call_soon_threadsafe(d["orch"].shutdown.set)
        for d in daemons:
            d["thread"].join(timeout=15)
            if not d["thread"].is_alive():  # a loop still running an agent can't be closed
                d["loop"].close()
                d["store"].close()
    return 0
