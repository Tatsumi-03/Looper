"""Drives the coding agent, one process per attempt: Claude Code in headless mode,
or Codex through `codex app-server`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from .config import AgentCfg

log = logging.getLogger("looper.agent")

#: the built-in reviewer reads the change; anything it wrote would land in the next commit
REVIEW_TOOLS = ["Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)",
                "Bash(git show:*)", "Bash(git status:*)"]


def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def tool_summary(block: dict) -> str:
    """'Bash git status' — a tool_use block as one short line."""
    args = block.get("input") or {}
    target = args.get("file_path") or args.get("command") or ""
    return f"{block.get('name', 'tool')} {str(target)[:60]}".strip()


def codex_item_summary(item: dict) -> str | None:
    """'$ git status' — a Codex command or file edit as one short line, else None."""
    if item.get("type") == "commandExecution":
        command = item.get("command") or ""
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = [command]
        if len(argv) == 3 and argv[1] in ("-c", "-lc"):  # codex wraps every command in `bash -lc`
            argv = [argv[2]]
        return f"$ {' '.join(argv)[:60]}"
    if item.get("type") == "fileChange":
        paths = [Path(c.get("path", "")).name for c in item.get("changes") or []]
        return f"edit {', '.join(paths)[:60]}"
    return None


@dataclass
class RunResult:
    ok: bool
    session_id: str | None
    cost_usd: float
    num_turns: int
    result_text: str
    subtype: str | None
    error: str | None
    log_path: Path
    duration_sec: float

    @property
    def summary(self) -> str:
        return (f"{'ok' if self.ok else 'FAILED'} subtype={self.subtype} turns={self.num_turns} "
                f"${self.cost_usd:.2f} in {self.duration_sec:.0f}s")


class ClaudeAgent:
    def __init__(self, cfg: AgentCfg, *, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run

    def build_argv(self, *, session_id: str | None, resume: bool) -> list[str]:
        c = self.cfg
        argv = [
            c.claude_bin, "-p",
            "--output-format", "stream-json",
            "--verbose",
            # anything outside the allowlist is denied instead of hanging a headless run
            "--permission-prompts", "none",
            "--model", c.model,
            *(["--effort", c.effort] if c.effort else []),
            "--max-turns", str(c.max_turns),
            "--max-budget-usd", str(c.max_budget_usd),
        ]
        if c.permission_mode == "bypassPermissions":
            argv.append("--dangerously-skip-permissions")
        else:
            argv += ["--permission-mode", c.permission_mode]
        if c.allowed_tools:
            argv += ["--allowedTools", *c.allowed_tools]
        if c.disallowed_tools:
            argv += ["--disallowedTools", *c.disallowed_tools]
        if resume and session_id:
            argv += ["--resume", session_id]
        elif session_id:
            argv += ["--session-id", session_id]
        return argv

    async def run(self, prompt: str, *, cwd: Path, log_path: Path,
                  session_id: str | None = None, resume: bool = False) -> RunResult:
        """Run one headless turn. Returns when the process exits or the timeout fires."""
        session_id = session_id or (None if resume else str(uuid.uuid4()))
        argv = self.build_argv(session_id=session_id, resume=resume)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        if self.dry_run:
            with log_path.open("a") as sink:
                sink.write(json.dumps({"dry_run": True, "argv": argv, "prompt": prompt}) + "\n")
            log.info("[dry-run] would run: %s (cwd=%s)", " ".join(argv[:8]) + " …", cwd)
            return RunResult(True, session_id, 0.0, 0, "[dry-run]", "dry_run", None, log_path, 0.0)

        log.info("claude %s in %s (session=%s)", "resume" if resume else "start", cwd, session_id)
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "CLAUDE_CODE_HARNESS": "looper"},
            start_new_session=True,  # own process group, so a timeout kills children too
        )

        state: dict[str, object] = {"session_id": session_id}
        stderr_tail: list[str] = []

        async def pump_stdout(fh) -> None:
            with log_path.open("a") as sink:  # a retried attempt adds to its transcript
                async for raw in fh:
                    line = raw.decode("utf-8", "replace")
                    sink.write(line)
                    sink.flush()
                    self._absorb(line, state)

        async def pump_stderr(fh) -> None:
            async for raw in fh:
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    stderr_tail.append(text)
                    del stderr_tail[:-40]

        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        async def heartbeat() -> None:
            """A silent terminal for minutes looks like a hang; say what it is doing."""
            while True:
                await asyncio.sleep(self.cfg.progress_every_sec)
                log.info("… still working in %s — %s, %d turns, last: %s",
                         Path(cwd).name, _mmss(time.monotonic() - started),
                         int(state.get("turns") or 0), state.get("activity") or "thinking")

        ticker = asyncio.create_task(heartbeat())
        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.gather(pump_stdout(proc.stdout), pump_stderr(proc.stderr), proc.wait()),
                timeout=self.cfg.run_timeout_sec,
            )
        except asyncio.TimeoutError:
            timed_out = True
            log.error("claude run timed out after %ss — killing", self.cfg.run_timeout_sec)
            self._kill(proc)
            await proc.wait()
        except asyncio.CancelledError:
            self._kill(proc)
            await proc.wait()
            raise
        finally:
            ticker.cancel()

        duration = time.monotonic() - started
        subtype = state.get("subtype")
        is_error = bool(state.get("is_error")) or timed_out or proc.returncode not in (0, None)
        error = None
        if timed_out:
            error = f"timeout after {self.cfg.run_timeout_sec}s"
        elif is_error:
            error = str(state.get("result") or "\n".join(stderr_tail[-5:]) or
                        f"exit {proc.returncode}")

        return RunResult(
            ok=not is_error,
            session_id=str(state.get("session_id") or "") or None,
            cost_usd=float(state.get("cost_usd") or 0.0),
            num_turns=int(state.get("num_turns") or 0),
            result_text=str(state.get("result") or ""),
            subtype=str(subtype) if subtype else None,
            error=error,
            log_path=log_path,
            duration_sec=duration,
        )

    @staticmethod
    def _absorb(line: str, state: dict[str, object]) -> None:
        line = line.strip()
        if not line or not line.startswith("{"):
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if sid := msg.get("session_id"):
            state["session_id"] = sid
        if msg.get("type") == "assistant":
            state["turns"] = int(state.get("turns") or 0) + 1
            for block in msg.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    state["activity"] = tool_summary(block)
        if msg.get("type") == "result":
            state["subtype"] = msg.get("subtype")
            state["is_error"] = msg.get("is_error", False)
            state["num_turns"] = msg.get("num_turns", 0)
            state["cost_usd"] = msg.get("total_cost_usd", 0.0)
            state["result"] = msg.get("result", "")

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass


#: codex's sandbox keeps .git read-only even inside writable roots, so the agent can't commit
CODEX_SOLVER_NOTE = ("Your sandbox keeps .git read-only and has no network: `git add`, "
                     "`git commit` and anything that downloads will fail. Skip them and leave "
                     "your changes in the working tree; Looper commits and pushes them.")


class CodexAgent:
    """One `codex app-server` process per attempt: start or resume a thread, run one
    turn, stop. The sandbox is the guard: workspace-write with no network, so the agent
    can't push or call GitHub; read-only for the reviewer. Codex reports no cost: it
    runs on the user's subscription, so cost_usd is always 0."""

    def __init__(self, cfg: AgentCfg, *, dry_run: bool = False, read_only: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.read_only = read_only

    def thread_params(self, cwd: Path) -> dict[str, object]:
        return {
            "cwd": str(cwd),
            "model": self.cfg.codex_model or None,
            "approvalPolicy": "never",  # nobody is there to approve: a blocked command just fails
            "sandbox": "read-only" if self.read_only else "workspace-write",
            "developerInstructions": None if self.read_only else CODEX_SOLVER_NOTE,
        }

    async def run(self, prompt: str, *, cwd: Path, log_path: Path,
                  session_id: str | None = None, resume: bool = False) -> RunResult:
        """Run one turn. A new thread ignores `session_id`: codex picks the thread id,
        and it comes back as the result's session_id."""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        argv = [self.cfg.codex_bin, "app-server"]

        if self.dry_run:
            with log_path.open("a") as sink:
                sink.write(json.dumps({"dry_run": True, "argv": argv, "prompt": prompt}) + "\n")
            log.info("[dry-run] would run: %s (cwd=%s)", " ".join(argv), cwd)
            return RunResult(True, session_id, 0.0, 0, "[dry-run]", "dry_run", None, log_path, 0.0)

        log.info("codex %s in %s (thread=%s)", "resume" if resume else "start", cwd,
                 session_id if resume else "new")
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,  # one message carries a whole turn's items
            start_new_session=True,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        state: dict[str, object] = {"items": 0, "text": "", "activity": None}
        stderr_tail: list[str] = []
        next_id = 0

        async def write(msg: dict[str, object]) -> None:
            proc.stdin.write((json.dumps(msg) + "\n").encode())  # type: ignore[union-attr]
            await proc.stdin.drain()  # type: ignore[union-attr]

        async def send(method: str, params: object = None, *, notify: bool = False) -> int:
            """A request (returns its id) or, with `notify`, a notification."""
            nonlocal next_id
            msg: dict[str, object] = {"method": method, "params": params}
            if not notify:
                next_id += 1
                msg["id"] = next_id
            await write(msg)
            return next_id

        async def pump_stderr() -> None:
            async for raw in proc.stderr:  # type: ignore[union-attr]
                if text := raw.decode("utf-8", "replace").rstrip():
                    stderr_tail.append(text)
                    del stderr_tail[:-40]

        async def converse(sink) -> dict[str, object]:
            """Handshake, thread, turn; returns the finished turn."""
            async def read(request_id: int | None) -> dict[str, object]:
                """Messages until the reply to `request_id`, or with None, until the
                turn completes. Returns that result or that turn."""
                async for raw in proc.stdout:  # type: ignore[union-attr]
                    sink.write(raw.decode("utf-8", "replace"))
                    sink.flush()
                    msg = json.loads(raw)
                    if "method" in msg and "id" in msg:  # an approval or a question: nobody is there
                        await write({"id": msg["id"], "error": {
                            "code": -32601, "message": "looper runs unattended"}})
                        continue
                    self._absorb(msg, state)
                    if request_id is None and "turn" in state:
                        return state["turn"]  # type: ignore[return-value]
                    if request_id is not None and msg.get("id") == request_id:
                        if err := msg.get("error"):
                            raise RuntimeError(err.get("message") or str(err))
                        return msg["result"]
                raise RuntimeError("codex app-server exited: " +
                                   ("\n".join(stderr_tail[-5:]) or f"exit {proc.returncode}"))

            await read(await send("initialize", {
                "clientInfo": {"name": "looper", "version": "0.1"}, "capabilities": None}))
            await send("initialized", notify=True)
            if resume and session_id:
                opened = await send("thread/resume", {"threadId": session_id, "excludeTurns": True,
                                                      **self.thread_params(cwd)})
            else:
                opened = await send("thread/start", self.thread_params(cwd))
            state["thread_id"] = (await read(opened))["thread"]["id"]  # type: ignore[index]
            await read(await send("turn/start", {
                "threadId": state["thread_id"],
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "effort": self.cfg.effort or None,
            }))
            return state["turn"] if "turn" in state else await read(None)  # type: ignore[return-value]

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self.cfg.progress_every_sec)
                log.info("… still working in %s — %s, %d items, last: %s",
                         Path(cwd).name, _mmss(time.monotonic() - started),
                         int(state["items"]), state["activity"] or "thinking")  # type: ignore[call-overload]

        ticker = asyncio.create_task(heartbeat())
        stderr_task = asyncio.create_task(pump_stderr())
        turn: dict[str, object] = {}
        error: str | None = None
        try:
            with log_path.open("a") as sink:
                turn = await asyncio.wait_for(converse(sink), timeout=self.cfg.run_timeout_sec)
        except asyncio.TimeoutError:
            error = f"timeout after {self.cfg.run_timeout_sec}s"
            log.error("codex run timed out after %ss — killing", self.cfg.run_timeout_sec)
        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            error = str(exc)
        finally:
            ticker.cancel()
            ClaudeAgent._kill(proc)
            await proc.wait()
            stderr_task.cancel()

        if not error and turn.get("status") != "completed":
            turn_error = turn.get("error") or {}
            error = (turn_error.get("message") if isinstance(turn_error, dict) else None) \
                or f"turn {turn.get('status')}"
        return RunResult(
            ok=error is None,
            session_id=str(state.get("thread_id") or session_id or "") or None,
            cost_usd=0.0,
            num_turns=int(state["items"]),  # type: ignore[call-overload]
            result_text=str(state["text"]),
            subtype=str(turn.get("status") or "error"),
            error=error,
            log_path=log_path,
            duration_sec=time.monotonic() - started,
        )

    @staticmethod
    def _absorb(msg: dict, state: dict[str, object]) -> None:
        """Fold one notification into `state`; the finished turn lands in state["turn"]."""
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                state["text"] = item.get("text") or ""  # the last message is the final answer
            elif summary := codex_item_summary(item):
                state["items"] = int(state["items"]) + 1  # type: ignore[call-overload]
                state["activity"] = summary
        elif method == "turn/completed" and params.get("threadId") == state.get("thread_id"):
            state["turn"] = params.get("turn") or {}


def make_agent(cfg: AgentCfg, *, dry_run: bool = False,
               review: bool = False) -> ClaudeAgent | CodexAgent:
    """The solver for [agent].provider, or with `review`, the read-only reviewer:
    a fresh session each round that can read but not edit, at medium effort."""
    if cfg.provider == "codex":
        return CodexAgent(replace(cfg, effort="medium") if review else cfg,
                          dry_run=dry_run, read_only=review)
    if review:
        # Sonnet whatever the solver runs: a weaker gate passes bugs or invents findings,
        # and every false finding costs the solver a whole revision round.
        cfg = replace(cfg, model="sonnet", effort="medium", allowed_tools=REVIEW_TOOLS,
                      disallowed_tools=[*cfg.disallowed_tools, "Edit", "Write", "NotebookEdit"])
    return ClaudeAgent(cfg, dry_run=dry_run)
