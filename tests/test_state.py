from Looper.state import State, Store


def test_task_lifecycle(tmp_path):
    s = Store(tmp_path / "h.db")
    t = s.create(4, "title")
    assert t.state == State.PENDING and not t.is_terminal

    s.set_state(4, State.SOLVING, branch="agent/issue-4")
    s.record_score(4, 3)
    s.record_score(4, 2)
    t = s.get(4)
    assert t.state == State.SOLVING and t.last_score == 2 and t.best_score == 3

    assert s.add_cost(4, 1.5) == 1.5
    assert s.add_cost(4, 0.5) == 2.0

    aid = s.start_attempt(4, 0, "solve", "sid")
    s.finish_attempt(aid, ok=True, num_turns=3, cost_usd=2.0, result_text="ok", log_path="p")
    row = s.attempts(4)[0]
    assert row["ok"] == 1 and row["num_turns"] == 3

    assert [x.issue_number for x in s.unfinished()] == [4]
    s.set_state(4, State.READY_FOR_HUMAN)
    assert s.unfinished() == [] and s.get(4).is_terminal
    assert any("READY_FOR_HUMAN" in (e["message"] or "") or e["state"] == "READY_FOR_HUMAN"
               for e in s.events(4))


def test_create_is_idempotent(tmp_path):
    s = Store(tmp_path / "h.db")
    s.create(9, "a")
    s.set_state(9, State.PR_OPEN, pr_number=12)
    again = s.create(9, "b")
    assert again.state == State.PR_OPEN and again.pr_number == 12


def test_same_issue_number_in_two_repos_keeps_separate_state(tmp_path):
    from Looper.config import Config, RepoCfg
    from Looper.cli import build

    a = Config(repo=RepoCfg(slug="acme/api"), root=tmp_path)
    b = Config(repo=RepoCfg(slug="acme/web"), root=tmp_path)
    store_a, _, wt_a, _ = build(a)
    store_b, _, wt_b, _ = build(b)

    store_a.create(12, "api bug")
    store_a.set_state(12, State.SOLVING)
    assert store_b.get(12) is None  # repo B's #12 is a different issue
    assert store_b.create(12, "web bug").state == State.PENDING
    assert store_a.get(12).title == "api bug"

    assert wt_a.path_for(12) != wt_b.path_for(12)
    assert a.logs_dir / "issue-12" != b.logs_dir / "issue-12"
    assert wt_a.repos_dir == wt_b.repos_dir and wt_a.base != wt_b.base  # one clone dir, a clone each
    store_a.close()
    store_b.close()
