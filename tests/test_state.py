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
