import asyncio
import curses
import threading
import time
from datetime import datetime, timezone

from Looper.config import Config, RepoCfg
from Looper.gh import GHError
from Looper.state import State, Store, Task
from Looper.tui import _start_daemon, act, ago, navigate, scroll_log, summary, visible_log


def test_ago_picks_largest_whole_unit():
    now = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert ago("2026-01-02T11:59:15+00:00", now) == "45s"
    assert ago("2026-01-02T11:48:00+00:00", now) == "12m"
    assert ago("2026-01-02T09:00:00+00:00", now) == "3h"
    assert ago("2025-12-31T11:00:00+00:00", now) == "2d"
    assert ago("", now) == "-"                           # task row without a timestamp


def test_summary_counts_by_attention_group():
    tasks = [Task(issue_number=n, state=s) for n, s in enumerate([
        State.SOLVING, State.REVISING, State.AWAITING_REVIEW, State.PARKED, State.PENDING])]
    assert summary(tasks) == "2 working · 1 review · 1 parked"   # PENDING and empty groups omitted


def test_visible_log_shows_tail_by_default():
    lines = [str(i) for i in range(20)]
    assert visible_log(lines, height=5, scroll=0) == ["15", "16", "17", "18", "19"]
    assert visible_log(lines, height=5, scroll=5) == ["10", "11", "12", "13", "14"]
    assert visible_log([], height=5, scroll=0) == []
    assert visible_log(lines, height=0, scroll=0) == []


def test_scroll_log_pages_and_clamps():
    total, height = 20, 5
    scroll = scroll_log(curses.KEY_PPAGE, total, height, 0)
    assert scroll == 5
    scroll = scroll_log(curses.KEY_PPAGE, total, height, scroll)
    assert scroll == 10  # top: total - height
    scroll = scroll_log(curses.KEY_PPAGE, total, height, scroll)
    assert scroll == 15                                   # clamped at top
    scroll = scroll_log(curses.KEY_NPAGE, total, height, scroll)
    assert scroll == 10
    scroll = scroll_log(curses.KEY_NPAGE, total, height, 2)
    assert scroll == 0                                    # clamped at live tail


def test_navigate_clamps_and_moves():
    assert navigate(-1, 0, 5) == 0                       # no tasks
    assert navigate(curses.KEY_DOWN, 3, 0) == 1
    assert navigate(curses.KEY_DOWN, 3, 2) == 2           # clamps at bottom
    assert navigate(curses.KEY_UP, 3, 0) == 0             # clamps at top
    assert navigate(-1, 2, 5) == 1                        # reclamp after shrink


def test_act_retry_and_abandon(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), root=tmp_path)
    store.create(1, "fix bug")
    store.set_state(1, State.PARKED, "parked for test", error="boom")

    msg = act(ord("r"), store.get(1), store, cfg, None, None)
    assert store.get(1).state == State.PENDING
    assert store.get(1).error is None
    assert "#1 ->" in msg

    msg = act(ord("x"), store.get(1), store, cfg, None, None)
    assert store.get(1).state == State.PARKED
    assert "parked" in msg
    store.close()


def test_act_start_agent_unparks_terminal_task(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), root=tmp_path)
    store.create(3, "wip")
    store.set_state(3, State.PARKED, "parked for test", error="boom")

    # no orch/loop wired up here — just checking the state side, spawn is a no-op
    msg = act(ord("a"), store.get(3), store, cfg, None, None)
    assert store.get(3).state == State.PENDING
    assert store.get(3).error is None
    assert "solving" in msg
    store.close()


def test_act_start_agent_leaves_active_task_alone(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), root=tmp_path)
    store.create(4, "wip")
    store.set_state(4, State.SOLVING, "already running")

    act(ord("a"), store.get(4), store, cfg, None, None)
    assert store.get(4).state == State.SOLVING  # not reset — it wasn't terminal
    store.close()


class _LabelGH:
    def __init__(self, fail_pr=False):
        self.removed = []
        self.fail_pr = fail_pr

    def remove_label(self, number, label):
        self.removed.append(("issue", number, label))

    def remove_pr_label(self, number, label):
        if self.fail_pr:
            raise GHError(["pr", "edit"], 1, "HTTP 403: Resource not accessible")
        self.removed.append(("pr", number, label))


def test_act_requeue_only_parked_and_drops_needs_human(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), root=tmp_path)
    orch = type("Orch", (), {"gh": _LabelGH()})()
    store.create(5, "wip")
    store.set_state(5, State.SOLVING, "running")

    msg = act(ord("e"), store.get(5), store, cfg, orch, None)
    assert store.get(5).state == State.SOLVING         # active task untouched
    assert orch.gh.removed == []
    assert "not requeued" in msg

    store.set_state(5, State.PARKED, "parked for test", error="boom", pr_number=42)
    act(ord("e"), store.get(5), store, cfg, orch, None)
    assert store.get(5).state == State.PR_OPEN         # keeps its PR, re-enters review
    assert store.get(5).error is None
    assert orch.gh.removed == [("issue", 5, "agent:needs-human"),
                               ("pr", 42, "agent:needs-human")]
    store.close()


def test_act_requeue_keeps_task_parked_when_label_removal_fails(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), root=tmp_path)
    orch = type("Orch", (), {"gh": _LabelGH(fail_pr=True)})()
    store.create(6, "wip")
    store.set_state(6, State.PARKED, "parked for test", error="boom", pr_number=43)

    msg = act(ord("e"), store.get(6), store, cfg, orch, None)
    assert store.get(6).state == State.PARKED          # still parked, so `e` can retry
    assert store.get(6).error == "boom"
    assert "still parked" in msg
    store.close()


def test_act_clean_refuses_active_task(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), root=tmp_path)
    store.create(2, "wip")
    msg = act(ord("c"), store.get(2), store, cfg, None, None)
    assert "not cleaned" in msg
    store.close()


class _FakeOrch:
    """Stands in for Orchestrator — just enough for _start_daemon's contract."""

    def __init__(self):
        self.shutdown = asyncio.Event()
        self.runs = 0

    async def run_daemon(self, *, install_signals=True):
        self.runs += 1
        await self.shutdown.wait()


def test_daemon_thread_toggles_off_and_on():
    loop = asyncio.new_event_loop()
    orch = _FakeOrch()
    thread = _start_daemon(loop, orch)
    time.sleep(0.05)
    assert thread.is_alive()

    loop.call_soon_threadsafe(orch.shutdown.set)
    thread.join(timeout=2)
    assert not thread.is_alive()  # 'd' toggles off

    thread = _start_daemon(loop, orch)  # 'd' toggles back on — same loop, fresh thread
    time.sleep(0.05)
    assert thread.is_alive()
    assert orch.runs == 2

    loop.call_soon_threadsafe(orch.shutdown.set)
    thread.join(timeout=2)
    loop.close()
