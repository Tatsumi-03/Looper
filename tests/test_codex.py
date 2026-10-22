import asyncio
import json
import sys

from Looper.agent import make_agent
from Looper.config import AgentCfg
from Looper.tui import transcript

# Stands in for `codex app-server`: answers the handshake, asks for one approval
# mid-turn and only finishes the turn once that request is answered.
FAKE_SERVER = r'''
import json, sys
def out(msg):
    print(json.dumps(msg), flush=True)
def notify(method, **params):
    out({"method": method, "params": params})
status = open("status").read()  # the test picks how the turn ends
for line in sys.stdin:
    msg = json.loads(line)
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        out({"id": msg["id"], "result": {}})
    elif method == "thread/start":
        out({"id": msg["id"], "result": {"thread": {"id": "thread-new"}}})
        open("thread_params.json", "w").write(json.dumps(params))
    elif method == "thread/resume":
        if params["threadId"] != "thread-old":
            out({"id": msg["id"], "error": {"code": -32600, "message": "no rollout found for thread id " + params["threadId"]}})
        else:
            out({"id": msg["id"], "result": {"thread": {"id": "thread-old"}}})
    elif method == "turn/start":
        out({"id": msg["id"], "result": {"turn": {"id": "t1", "status": "inProgress"}}})
        tid = params["threadId"]
        out({"id": 900, "method": "item/commandExecution/requestApproval", "params": {}})
    elif msg.get("id") == 900:
        assert "error" in msg, msg
        notify("item/completed", threadId=tid, item={"type": "agentMessage", "text": "looking"})
        notify("item/completed", threadId=tid, item={"type": "commandExecution", "command": "/usr/bin/bash -lc 'git status'"})
        notify("item/completed", threadId=tid, item={"type": "agentMessage", "text": "done <pr-description>b</pr-description>"})
        notify("turn/completed", threadId="someone-else", turn={"status": "failed"})
        error = {"message": "usage limit reached"} if status == "failed" else None
        notify("turn/completed", threadId=tid, turn={"id": "t1", "status": status, "error": error})
'''


def run_codex(tmp_path, *, status="completed", review=False, **kwargs):
    server = tmp_path / "codex"
    server.write_text(f"#!{sys.executable}\n{FAKE_SERVER}")
    server.chmod(0o755)
    (tmp_path / "status").write_text(status)
    cfg = AgentCfg(provider="codex", codex_bin=str(server), model="", run_timeout_sec=10)
    return asyncio.run(make_agent(cfg, review=review).run(
        "do it", cwd=tmp_path, log_path=tmp_path / "logs" / "00-solve.jsonl", **kwargs))


def test_new_thread_returns_final_message_and_codex_thread_id(tmp_path):
    result = run_codex(tmp_path, session_id="looper-uuid")
    assert result.ok and result.error is None
    assert result.session_id == "thread-new"          # codex names the thread, not looper
    assert result.result_text == "done <pr-description>b</pr-description>"
    assert result.num_turns == 1 and result.cost_usd == 0.0
    params = json.loads((tmp_path / "thread_params.json").read_text())
    assert (params["sandbox"], params["approvalPolicy"]) == ("workspace-write", "never")
    lines = transcript(tmp_path / "logs").splitlines()
    assert lines[1:4] == ["looking", "→ $ git status", "done <pr-description>b</pr-description>"]
    assert lines[-1] == "── completed"


def test_reviewer_thread_is_read_only(tmp_path):
    assert run_codex(tmp_path, review=True).ok
    params = json.loads((tmp_path / "thread_params.json").read_text())
    assert params["sandbox"] == "read-only" and params["developerInstructions"] is None


def test_resume_keeps_thread_and_unknown_thread_is_a_session_error(tmp_path):
    resumed = run_codex(tmp_path, session_id="thread-old", resume=True)
    assert resumed.ok and resumed.session_id == "thread-old"
    lost = run_codex(tmp_path, session_id="thread-gone", resume=True)
    assert not lost.ok and "no rollout found" in lost.error   # what _is_session_error retries on


def test_failed_turn_is_not_ok(tmp_path):
    result = run_codex(tmp_path, status="failed")
    assert not result.ok and result.error == "usage limit reached"
