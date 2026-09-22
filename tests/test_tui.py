import curses

from Looper.config import Config, LoopCfg, RepoCfg
from Looper.state import State, Store
from Looper.tui import act, navigate, sync_issues


class FakeGH:
    def __init__(self, issues):
        self._issues = issues

    def list_open_issues(self):
        return self._issues


def test_navigate_clamps_and_moves():
    assert navigate(-1, 0, 5) == 0                       # no tasks
    assert navigate(curses.KEY_DOWN, 3, 0) == 1
    assert navigate(curses.KEY_DOWN, 3, 2) == 2           # clamps at bottom
    assert navigate(curses.KEY_UP, 3, 0) == 0             # clamps at top
    assert navigate(-1, 2, 5) == 1                        # reclamp after shrink


def test_act_retry_and_abandon(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), root=tmp_path)
    task = store.create(1, "fix bug")
    store.set_state(1, State.PARKED, "parked for test", error="boom")

    msg = act(ord("r"), store.get(1), store, cfg)
    assert store.get(1).state == State.PENDING
    assert store.get(1).error is None
    assert "#1 ->" in msg

    msg = act(ord("a"), store.get(1), store, cfg)
    assert store.get(1).state == State.PARKED
    assert "parked" in msg
    store.close()


def test_sync_issues_creates_new_and_skips_labelled_and_tracked(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), loop=LoopCfg(skip_labels=["wontfix"]), root=tmp_path)
    store.create(1, "already tracked")  # pre-existing task
    gh = FakeGH([
        {"number": 1, "title": "already tracked", "labels": []},
        {"number": 2, "title": "new issue", "labels": []},
        {"number": 3, "title": "skip me", "labels": [{"name": "wontfix"}]},
    ])

    created = sync_issues(gh, cfg, store)

    assert created == 1
    assert store.get(2) is not None
    assert store.get(3) is None
    store.close()


def test_act_clean_refuses_active_task(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), root=tmp_path)
    store.create(2, "wip")
    msg = act(ord("c"), store.get(2), store, cfg)
    assert "not cleaned" in msg
    store.close()
