from Looper.ci import verdict


def test_verdict_reads_both_check_runs_and_commit_statuses():
    ok = {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"}
    skipped = {"__typename": "CheckRun", "name": "deploy", "status": "COMPLETED",
               "conclusion": "SKIPPED"}
    running = {"__typename": "CheckRun", "name": "tests", "status": "IN_PROGRESS",
               "conclusion": None}
    red_status = {"__typename": "StatusContext", "context": "ci/legacy", "state": "ERROR"}
    waiting_status = {"__typename": "StatusContext", "context": "ci/other", "state": "PENDING"}

    assert verdict([]) == (False, [])                       # no CI at all reads as green
    assert verdict([ok, skipped]) == (False, [])
    assert verdict([ok, running]) == (True, [])
    assert verdict([ok, red_status]) == (False, [red_status])
    assert verdict([waiting_status, red_status]) == (True, [red_status])
