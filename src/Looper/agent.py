"""Drives Claude Code in headless mode, one process per attempt."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import AgentCfg

log = logging.getLogger("looper.agent")


def _mmss(seconds: float) -> str:
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def tool_summary(block: dict) -> str:
    """'Bash git status' — a tool_use block as one short line."""
    args = block.get("input") or {}
    target = args.get("file_path") or args.get("command") or ""
    return f"{block.get('name', 'tool')} {str(target)[:60]}".strip()


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
            log_path.write_text(json.dumps({"dry_run": True, "argv": argv, "prompt": prompt}) + "\n")
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
            with log_path.open("w") as sink:
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
