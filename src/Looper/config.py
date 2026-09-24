"""Configuration loading and validation."""

from __future__ import annotations

import json
import os
import re
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
    model: str = ""  # empty = [agent].model


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
    repos: list[RepoCfg] = field(default_factory=list)  # every [[repos]] entry
    root: Path = field(default_factory=home)
    path: Path | None = None

    def select(self, slug: str) -> None:
        """Make `slug` the repo this run works on. Once per loaded config: a repo's
        model replaces [agent].model."""
        repo = next((r for r in self.repos if r.slug.lower() == slug.lower()), None)
        if repo is None:
            known = ", ".join(r.slug for r in self.repos) or "none yet"
            raise ConfigError(f"{slug} is not in {self.path}, known repos: {known}. "
                              f"Add it with `looper init`")
        self.repo = repo
        if repo.model:
            self.agent.model = repo.model

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
    if "repo" in raw:
        raise ConfigError(f"{cfg_path}: [repo] is now [[repos]], one entry per repository. "
                          "Rename the header, or add repos with `looper init`")
    entries = raw.get("repos", [])
    if not isinstance(entries, list):
        raise ConfigError("repos must be [[repos]] entries")
    cfg.repos = [_build(RepoCfg, entry, "repos") for entry in entries]
    for name in ("agent", "loop", "greptile", "safety"):
        if name in raw:
            section = raw[name]
            if not isinstance(section, dict):
                raise ConfigError(f"[{name}] must be a table")
            cls = type(getattr(cfg, name))
            setattr(cfg, name, _build(cls, section, name))
    unknown = set(raw) - {"repos", "agent", "loop", "greptile", "safety"}
    if unknown:
        raise ConfigError(f"unknown config sections: {', '.join(sorted(unknown))}")

    validate(cfg)
    return cfg  # no repo selected yet: that's the command's call (cli._load, the tui picker)


_REPO_BLOCK = re.compile(r"^\[\[repos\]\][ \t]*\n(?:[ \t]*\w+[ \t]*=.*(?:\n|$))*", re.M)


def upsert_repo(text: str, repo: RepoCfg) -> str:
    """looper.toml text with `repo`'s [[repos]] entry replaced, or appended if new.
    Only the entry's own `key = value` lines are touched; comments and every
    other section stay exactly as they were."""
    lines = ["[[repos]]", f"slug = {json.dumps(repo.slug)}",
             f"base_branch = {json.dumps(repo.base_branch)}"]
    if repo.model:
        lines.append(f"model = {json.dumps(repo.model)}")
    block = "\n".join(lines) + "\n"
    for m in _REPO_BLOCK.finditer(text):
        slug = tomllib.loads(m.group())["repos"][0].get("slug", "")
        if slug.lower() == repo.slug.lower():
            return text[:m.start()] + block + text[m.end():]
    return text.rstrip("\n") + "\n\n" + block


def validate(cfg: Config) -> None:
    for repo in cfg.repos:
        if repo.slug.count("/") != 1 or not all(repo.slug.split("/")):
            raise ConfigError(f"[[repos]] slug must be 'owner/name', got {repo.slug!r}")
    slugs = [r.slug.lower() for r in cfg.repos]
    if len(slugs) != len(set(slugs)):
        raise ConfigError("the same repo appears twice in [[repos]]")
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
