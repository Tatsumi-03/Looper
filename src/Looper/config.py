"""Configuration loading and validation."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

PERMISSION_MODES = {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}


class ConfigError(Exception):
    pass


def home() -> Path:
    """Where looper.toml and var/ live: $LOOPER_HOME (the ./looper launcher sets it
    to the checkout), else the current directory."""
    return Path(os.environ.get("LOOPER_HOME") or Path.cwd())


@dataclass
class RepoCfg:
    slug: str = ""
    base_branch: str = "main"


@dataclass
class AgentCfg:
    claude_bin: str = "claude"
    model: str = "sonnet"
    max_turns: int = 60
    max_budget_usd: float = 5.0
    run_timeout_sec: int = 3600
    progress_every_sec: int = 30
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)


@dataclass
class LoopCfg:
    max_concurrent_agents: int = 3
    max_iterations: int = 5
    max_open_prs: int = 5
    issue_poll_sec: int = 60
    skip_labels: list[str] = field(default_factory=lambda: ["no-agent", "wontfix", "blocked"])
    ci_timeout_sec: int = 1800  # how long a 5/5 PR may wait for checks before parking
    only_labels: list[str] = field(default_factory=lambda: ["agent"])  # empty = every open issue


@dataclass
class GreptileCfg:
    bot_logins: list[str] = field(default_factory=lambda: ["greptile-apps[bot]", "greptile[bot]"])
    poll_interval_sec: int = 20
    review_timeout_sec: int = 1200
    max_retriggers: int = 2
    retrigger_body: str = "@greptileai review"
    target_score: int = 5
    fake_scores: list[int] = field(default_factory=list)


@dataclass
class SafetyCfg:
    dry_run: bool = False
    branch_prefix: str = "agent/issue-"
    auto_merge: bool = False
    max_cost_per_issue_usd: float = 25.0


@dataclass
class Config:
    repo: RepoCfg = field(default_factory=RepoCfg)
    agent: AgentCfg = field(default_factory=AgentCfg)
    loop: LoopCfg = field(default_factory=LoopCfg)
    greptile: GreptileCfg = field(default_factory=GreptileCfg)
    safety: SafetyCfg = field(default_factory=SafetyCfg)
    root: Path = field(default_factory=home)
    path: Path | None = None

    # --- derived ---------------------------------------------------------
    @property
    def owner(self) -> str:
        return self.repo.slug.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repo.slug.split("/", 1)[1]

    @property
    def var_dir(self) -> Path:
        """This repo's state: db, logs, worktrees, locks. An issue number only means
        something inside one repo, so each repo gets its own folder."""
        return self.root / "var" / self.repo.slug.replace("/", "__")

    @property
    def db_path(self) -> Path:
        return self.var_dir / "looper.db"

    @property
    def logs_dir(self) -> Path:
        return self.var_dir / "logs"

    @property
    def worktrees_dir(self) -> Path:
        return self.var_dir / "worktrees"

    @property
    def repos_dir(self) -> Path:
        return self.root / "var" / "repos"  # clones are shared, each already named by slug

    def branch_for(self, issue: int) -> str:
        return f"{self.safety.branch_prefix}{issue}"

    def ensure_dirs(self) -> None:
        for d in (self.var_dir, self.logs_dir, self.worktrees_dir, self.repos_dir):
            d.mkdir(parents=True, exist_ok=True)


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _build(cls: type, data: dict[str, Any], section: str) -> Any:
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"[{section}] has unknown keys: {', '.join(sorted(unknown))}")
    kwargs = {k: _expand(v) for k, v in data.items()}
    return cls(**kwargs)


def load(path: str | Path | None = None, root: Path | None = None) -> Config:
    """Load looper.toml. Falls back to <home>/looper.toml (see home())."""
    root = Path(root or home()).resolve()
    cfg_path = Path(path) if path else root / "looper.toml"
    if not cfg_path.exists():
        raise ConfigError(
            f"no config at {cfg_path} — copy looper.toml.example or run `looper init`"
        )
    raw = tomllib.loads(cfg_path.read_text())

    cfg = Config(root=root, path=cfg_path)
    for name in ("repo", "agent", "loop", "greptile", "safety"):
        if name in raw:
            section = raw[name]
            if not isinstance(section, dict):
                raise ConfigError(f"[{name}] must be a table")
            cls = type(getattr(cfg, name))
            setattr(cfg, name, _build(cls, section, name))
    unknown = set(raw) - {"repo", "agent", "loop", "greptile", "safety"}
    if unknown:
        raise ConfigError(f"unknown config sections: {', '.join(sorted(unknown))}")

    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.repo.slug.count("/") != 1 or not all(cfg.repo.slug.split("/")):
        raise ConfigError(f"repo.slug must be 'owner/name', got {cfg.repo.slug!r}")
    if cfg.agent.permission_mode not in PERMISSION_MODES:
        raise ConfigError(
            f"agent.permission_mode must be one of {sorted(PERMISSION_MODES)}, "
            f"got {cfg.agent.permission_mode!r}"
        )
    if not cfg.safety.branch_prefix:
        raise ConfigError("safety.branch_prefix must not be empty (it is the push guard)")
    if cfg.loop.max_concurrent_agents < 1:
        raise ConfigError("loop.max_concurrent_agents must be >= 1")
    if cfg.loop.max_iterations < 1:
        raise ConfigError("loop.max_iterations must be >= 1")
    if not 0 <= cfg.greptile.target_score <= 5:
        raise ConfigError("greptile.target_score must be 0..5")
    for s in cfg.greptile.fake_scores:
        if not 0 <= s <= 5:
            raise ConfigError("greptile.fake_scores entries must be 0..5")
    assert is_dataclass(cfg)
