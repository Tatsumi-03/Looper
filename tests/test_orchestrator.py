"""Unit tests for the poll-cycle helpers — the full FSM lives in test_flow.py."""

from Looper.config import Config, LoopCfg, RepoCfg
from Looper.orchestrator import check_merged, trackable_issues
from Looper.state import State, Store


class FakeGH:
    def __init__(self, issues=None, prs=None):
        self._issues = issues or []
        self._prs = prs or {}

    def list_open_issues(self):
        return self._issues

    def pr(self, number):
        return self._prs.get(number)


def test_trackable_issues_skips_labelled_and_claimed(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), loop=LoopCfg(skip_labels=["wontfix"]), root=tmp_path)
    gh = FakeGH(issues=[
        {"number": 1, "title": "plain", "labels": []},
        {"number": 2, "title": "skip me", "labels": [{"name": "wontfix"}]},
        {"number": 3, "title": "another runner has it",
         "labels": [{"name": "agent:in-progress"}]},
    ])

    assert [i["number"] for i in trackable_issues(gh, cfg, store)] == [1]
    store.close()


def test_trackable_issues_keeps_our_own_in_progress_task(tmp_path):
    store = Store(tmp_path / "looper.db")
    cfg = Config(repo=RepoCfg(slug="o/n"), root=tmp_path)
    store.create(3, "ours")  # we labelled it, so it is not another runner's
    gh = FakeGH(issues=[{"number": 3, "title": "ours",
                         "labels": [{"name": "agent:in-progress"}]}])

    assert [i["number"] for i in trackable_issues(gh, cfg, store)] == [3]
    store.close()


def test_check_merged_only_flips_finished_tasks(tmp_path):
    store = Store(tmp_path / "looper.db")
    store.create(1, "landed")
    store.set_state(1, State.READY_FOR_HUMAN, "5/5", pr_number=42)
    store.create(2, "still going")
    store.set_state(2, State.SOLVING, "working", pr_number=43)
    store.create(3, "parked but merged anyway")
    store.set_state(3, State.PARKED, "gave up", pr_number=44)
    gh = FakeGH(prs={42: {"state": "MERGED"}, 43: {"state": "MERGED"}, 44: {"state": "MERGED"}})

    assert check_merged(gh, store) == 2
    assert store.get(1).state == State.MERGED
    assert store.get(2).state == State.SOLVING   # in flight — left alone
    assert store.get(3).state == State.MERGED    # a human can merge a parked PR
    store.close()


def test_check_merged_ignores_unmerged_and_repeat_calls(tmp_path):
    store = Store(tmp_path / "looper.db")
    store.create(1, "waiting on a human")
    store.set_state(1, State.READY_FOR_HUMAN, "5/5", pr_number=42)
    gh = FakeGH(prs={42: {"state": "OPEN"}})

    assert check_merged(gh, store) == 0
    assert store.get(1).state == State.READY_FOR_HUMAN

    gh._prs[42] = {"state": "MERGED"}
    assert check_merged(gh, store) == 1
    assert check_merged(gh, store) == 0          # already MERGED, not re-counted
    store.close()
