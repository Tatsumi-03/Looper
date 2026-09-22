import curses

from Looper.config import Config, RepoCfg
from Looper.state import State, Store
from Looper.tui import act, navigate, scroll_log, visible_log


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


def test_act_clean_refuses_active_task(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), root=tmp_path)
    store.create(2, "wip")
    msg = act(ord("c"), store.get(2), store, cfg, None, None)
    assert "not cleaned" in msg
    store.close()
