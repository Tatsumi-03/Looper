"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import tomllib
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as config_mod
from .agent import ClaudeAgent
from .config import Config, ConfigError
from .gh import GH, GHError
from .orchestrator import Orchestrator
from .state import State, Store
from .worktree import Worktrees, file_lock

EXAMPLE_CANDIDATES = (
    Path(__file__).parents[2] / "looper.toml.example",
    Path(__file__).parent / "looper.toml.example",
    Path.cwd() / "looper.toml.example",
)


def _example() -> Path | None:
    return next((p for p in EXAMPLE_CANDIDATES if p.exists()), None)


def setup_logging(verbose: bool) -> None:
    # line buffering matters: without it a redirected daemon looks dead for minutes
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def build(cfg: Config) -> tuple[Store, GH, Worktrees, Orchestrator]:
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    gh = GH(cfg.repo.slug, dry_run=cfg.safety.dry_run)
    wt = Worktrees(
        cfg.repo.slug,
        base_branch=cfg.repo.base_branch,
        repos_dir=cfg.repos_dir,
        worktrees_dir=cfg.worktrees_dir,
        branch_prefix=cfg.safety.branch_prefix,
        dry_run=cfg.safety.dry_run,
    )
    agent = ClaudeAgent(cfg.agent, dry_run=cfg.safety.dry_run)
    return store, gh, wt, Orchestrator(cfg, store, gh, wt, agent)


# --------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------- #
MODEL_ALIASES = ("sonnet", "opus", "haiku")
GITHUB_PREFIXES = ("https://github.com/", "http://github.com/", "git@github.com:", "github.com/")


def _ask(question: str, default: str = "") -> str:
    answer = input(f"{question} [{default}]: " if default else f"{question}: ").strip()
    return answer or default


def _answer(flag: str | None, question: str, default: str, check):
    """The flag's value, else ask until `check` accepts. `check` returns the value to
    keep, or prints why not and returns None. A bad flag fails instead of asking."""
    while True:
        value = check(flag or _ask(question, default))
        if value is not None or flag:
            return value


def repo_slug(text: str) -> str:
    """'acme/api', 'https://github.com/acme/api' or 'git@github.com:acme/api.git' -> 'acme/api'."""
    s = text.strip().rstrip("/").removesuffix(".git")
    return next((s[len(p):] for p in GITHUB_PREFIXES if s.startswith(p)), s)


def _cwd_repo() -> str:
    """owner/name of the GitHub repo the current folder is a clone of, else ''."""
    proc = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner",
                           "--jq", ".nameWithOwner"], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _say_no(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr)


def _check_repo(answer: str) -> dict | None:
    slug = repo_slug(answer)
    if slug.count("/") != 1 or not all(slug.split("/")):
        return _say_no("expected owner/name, e.g. acme/api")
    try:
        return GH(slug).api(f"repos/{slug}")
    except GHError as exc:
        reason = (exc.stderr.strip().splitlines() or ["no reason given"])[-1]
        return _say_no(f"can't open {slug} on GitHub: {reason}")


def _check_model(answer: str) -> str | None:
    if answer in MODEL_ALIASES or answer.startswith("claude-"):
        return answer
    return _say_no(f"unknown model {answer!r}: use {', '.join(MODEL_ALIASES)} or a claude-… id")


def cmd_init(args: argparse.Namespace) -> int:
    """Add a repo to looper.toml, or update it if it's already there: ask for the repo,
    base branch and model (flags skip each question) and check them against GitHub.
    Writes the shared settings from the template on first run, creates the opt-in
    label, then runs doctor for that repo."""
    target = Path(args.config) if args.config else config_mod.home() / "looper.toml"
    example = _example()
    if example is None:
        print("template looper.toml.example not found", file=sys.stderr)
        return 1
    if not shutil.which("gh"):
        print("gh not found on PATH, install it first: https://cli.github.com", file=sys.stderr)
        return 1
    # an unreadable looper.toml raises here: fix it rather than have init guess
    cfg = config_mod.load(target) if target.exists() else None
    text = target.read_text() if cfg else example.read_text()

    info = _answer(args.repo, "Repository (owner/name or GitHub URL)",
                   "" if args.repo else _cwd_repo(), _check_repo)
    if info is None:
        return 1
    repo = info["full_name"]
    gh = GH(repo)
    known = next((r for r in cfg.repos if r.slug.lower() == repo.lower()), None) if cfg else None
    if known:
        print(f"  {repo} is already set up, Enter keeps each current answer")

    def check_branch(name: str) -> str | None:
        try:
            gh.api(f"repos/{repo}/branches/{name}")
            return name
        except GHError:
            return _say_no(f"{repo} has no branch {name!r}")

    base = _answer(args.base_branch, "Base branch (PRs target this)",
                   known.base_branch if known else info["default_branch"], check_branch)
    model = _answer(args.model, f"Model ({'/'.join(MODEL_ALIASES)})",
                    (known and known.model) or (cfg.agent.model if cfg else "sonnet"), _check_model)
    if base is None or model is None:
        return 1

    text = config_mod.upsert_repo(text, config_mod.RepoCfg(repo, base, model))
    tomllib.loads(text)  # never write a file the next command can't read
    target.write_text(text)
    print(f"\n{'updated' if known else 'added'} {repo} in {target}: base {base}, model {model}")
    if not known:  # gh can't see app installs without app auth, so just remind
        print(f"make sure the Greptile GitHub app is installed on {repo}, "
              "or no PR will get a review: https://app.greptile.com")
    for label in (cfg.loop if cfg else config_mod.LoopCfg()).only_labels:
        if gh.ensure_label(label):
            print(f'label "{label}" is on {repo}, add it to an issue to queue that issue')
        else:
            print(f'could not create label "{label}" on {repo}, add it on GitHub yourself')
    print()
    args.repo = repo  # doctor checks the repo just added
    return cmd_doctor(args)


def cmd_daemon(args: argparse.Namespace) -> int:
    cfg = _load(args)
    _apply_overrides(cfg, args)
    lock = cfg.var_dir / "daemon.lock"
    cfg.ensure_dirs()
    try:
        with file_lock(lock):
            store, _gh, _wt, orch = build(cfg)
            try:
                asyncio.run(orch.run_daemon())
            finally:
                store.close()
    except Exception as exc:
        if "locked by another" in str(exc):
            print("another looper daemon is already running", file=sys.stderr)
            return 1
        raise
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    cfg = _load(args)
    _apply_overrides(cfg, args)
    store, _gh, _wt, orch = build(cfg)
    try:
        task = asyncio.run(orch.run_once(args.issue))
    finally:
        store.close()
    if task is None:
        return 1
    print(f"#{args.issue} -> {task.state} (PR {task.pr_number}, best {task.best_score}/5, "
          f"${task.cost_usd:.2f})")
    return 0 if task.state == State.READY_FOR_HUMAN else 2


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load(args)
    store = Store(cfg.db_path)
    tasks = store.all_tasks()
    if not tasks:
        print("no tasks yet")
        return 0
    header = f"{'ISSUE':>6}  {'STATE':<16} {'PR':>5}  {'IT':>2} {'SCORE':>5} {'BEST':>4} " \
             f"{'COST':>7}  TITLE"
    print(header)
    print("-" * max(len(header), 80))
    for t in tasks:
        score = f"{t.last_score}/5" if t.last_score is not None else "-"
        best = f"{t.best_score}" if t.best_score is not None else "-"
        print(f"{t.issue_number:>6}  {t.state:<16} {t.pr_number or '-':>5}  {t.iteration:>2} "
              f"{score:>5} {best:>4} {'$%.2f' % t.cost_usd:>7}  {t.title[:44]}")
        if t.error:
            print(f"{'':>6}  └─ {t.error[:100]}")
    store.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cfg = _load(args)
    store = Store(cfg.db_path)
    task = store.get(args.issue)
    if not task:
        print(f"no task for #{args.issue}", file=sys.stderr)
        return 1
    print(f"#{task.issue_number} {task.title}")
    for key in ("state", "branch", "pr_number", "head_sha", "iteration", "last_score",
                "best_score", "retriggers", "cost_usd", "session_id", "error", "updated_at"):
        print(f"  {key:<12} {getattr(task, key)}")
    print("\nevents (newest first):")
    for e in store.events(args.issue, limit=args.limit):
        print(f"  {e['ts']}  {e['state'] or '':<16} {e['message']}")
    print("\nlogs:", cfg.logs_dir / f"issue-{args.issue}")
    store.close()
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    cfg = _load(args)
    store = Store(cfg.db_path)
    task = store.get(args.issue)
    if not task:
        print(f"no task for #{args.issue}", file=sys.stderr)
        return 1
    fields: dict[str, object] = {"error": None}
    if args.restart:
        fields |= {"pr_number": None, "session_id": None, "iteration": 0, "head_sha": None,
                   "pushed_at": None, "retriggers": 0}
    state = State.PR_OPEN if (task.pr_number and not args.restart) else State.PENDING
    store.set_state(args.issue, state, "manual retry", **fields)
    print(f"#{args.issue} reset to {state}")
    store.close()
    return 0


def cmd_abandon(args: argparse.Namespace) -> int:
    cfg = _load(args)
    store = Store(cfg.db_path)
    store.set_state(args.issue, State.PARKED, "manually abandoned", error="abandoned by operator")
    if args.clean:
        _, _gh, wt, _o = build(cfg)
        wt.remove(args.issue)
        print(f"removed worktree for #{args.issue}")
    store.close()
    print(f"#{args.issue} parked")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    cfg = _load(args)
    store = Store(cfg.db_path)
    _, _gh, wt, _o = build(cfg)
    removed = 0
    for task in store.all_tasks():
        if task.is_terminal and wt.path_for(task.issue_number).exists():
            if task.state == State.PARKED and not args.all:
                continue
            wt.remove(task.issue_number)
            removed += 1
    store.close()
    print(f"removed {removed} worktree(s)")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    if args.repo:
        cfg.select(args.repo)
    from . import tui
    return tui.run(cfg, pick=not args.repo)


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    try:
        cfg = _load(args)
        print(f"config    ok   {cfg.path} -> {cfg.repo.slug} (base {cfg.repo.base_branch})")
    except ConfigError as exc:
        print(f"config    FAIL {exc}")
        return 1
    for name, path in (("gh", shutil.which("gh")), ("git", shutil.which("git")),
                       ("claude", shutil.which(cfg.agent.claude_bin))):
        print(f"{name:<9} {'ok  ' if path else 'FAIL'} {path or 'not found on PATH'}")
        ok &= bool(path)
    gh = GH(cfg.repo.slug)
    try:
        gh.preflight()
        print(f"github    ok   authenticated as {gh.whoami()}, {cfg.repo.slug} reachable")
    except GHError as exc:
        print(f"github    FAIL {exc}")
        ok = False
    if cfg.safety.dry_run:
        print("safety    note dry_run is ON — nothing will be pushed or posted")
    if cfg.greptile.fake_scores:
        print(f"safety    note fake_scores={cfg.greptile.fake_scores} — Greptile will not be consulted")
    return 0 if ok else 1


def _load(args: argparse.Namespace) -> Config:
    """looper.toml with the repo this command works on selected: --repo, or the
    only repo there is."""
    cfg = config_mod.load(args.config)
    if args.repo or len(cfg.repos) == 1:
        cfg.select(args.repo or cfg.repos[0].slug)
    else:
        if not cfg.repos:
            raise ConfigError(f"no repos in {cfg.path} yet, add one with `looper init`")
        raise ConfigError(f"{len(cfg.repos)} repos in {cfg.path}, pick one with --repo: "
                          + ", ".join(r.slug for r in cfg.repos))
    return cfg


def _apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    if getattr(args, "dry_run", False):
        cfg.safety.dry_run = True
    if getattr(args, "fake_review", None):
        cfg.greptile.fake_scores = [int(s) for s in args.fake_review.split(",")]
    if getattr(args, "concurrency", None):
        cfg.loop.max_concurrent_agents = args.concurrency
    if getattr(args, "model", None):
        cfg.agent.model = args.model
    config_mod.validate(cfg)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        "looper",
        description="Drive Claude Code agents from GitHub issue to a Greptile-approved PR.",
        epilog=("examples:\n"
                "  looper doctor                 check config, gh, git and claude\n"
                "  looper once 12                work one issue, start to finish\n"
                "  looper once 12 --dry-run      rehearse: no push, no PR, no comments\n"
                "  looper once 12 --fake-review 3,4,5\n"
                "                                 exercise the revise loop without Greptile\n"
                "  looper daemon                 work every open issue, continuously\n"
                "  looper status                 what every task is doing\n"
                "  looper show 12                event log and spend for one issue"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-c", "--config", help="path to looper.toml (default: in the Looper home)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def repo_flag(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-r", "--repo", metavar="owner/name",
                            help="which repo in looper.toml (needed once there are several)")

    def runner(parser: argparse.ArgumentParser) -> None:
        repo_flag(parser)
        parser.add_argument("--dry-run", action="store_true",
                            help="do everything except push, PR, and comment")
        parser.add_argument("--fake-review", metavar="3,4,5",
                            help="inject canned Greptile scores instead of polling")
        parser.add_argument("--model", help="override the agent model")

    d = sub.add_parser("daemon", help="watch open issues and work them continuously")
    runner(d)
    d.add_argument("--concurrency", type=int, help="override max_concurrent_agents")
    d.set_defaults(fn=cmd_daemon)

    o = sub.add_parser("once", help="run one issue end to end in the foreground")
    o.add_argument("issue", type=int)
    runner(o)
    o.set_defaults(fn=cmd_once)

    i = sub.add_parser("init", help="add a repo to looper.toml (asks for repo, branch, model)")
    i.add_argument("--repo", metavar="owner/name", help="skip the repo question")
    i.add_argument("--model", help="skip the model question")
    i.add_argument("--base-branch", help="default: the repo's default branch")
    i.set_defaults(fn=cmd_init)

    s = sub.add_parser("status", help="table of all tasks")
    repo_flag(s)
    s.set_defaults(fn=cmd_status)

    sh = sub.add_parser("show", help="detail and event log for one issue")
    repo_flag(sh)
    sh.add_argument("issue", type=int)
    sh.add_argument("--limit", type=int, default=40)
    sh.set_defaults(fn=cmd_show)

    r = sub.add_parser("retry", help="un-park a task")
    repo_flag(r)
    r.add_argument("issue", type=int)
    r.add_argument("--restart", action="store_true",
                   help="also forget the PR and session, starting from scratch")
    r.set_defaults(fn=cmd_retry)

    a = sub.add_parser("abandon", help="park a task manually")
    repo_flag(a)
    a.add_argument("issue", type=int)
    a.add_argument("--clean", action="store_true", help="also remove the worktree")
    a.set_defaults(fn=cmd_abandon)

    c = sub.add_parser("clean", help="remove worktrees of finished tasks")
    repo_flag(c)
    c.add_argument("--all", action="store_true", help="include parked tasks")
    c.set_defaults(fn=cmd_clean)

    doc = sub.add_parser("doctor", help="check config, tools and GitHub access")
    repo_flag(doc)
    doc.set_defaults(fn=cmd_doctor)

    tu = sub.add_parser("tui", help="live dashboard; asks which repo unless --repo")
    repo_flag(tu)
    tu.set_defaults(fn=cmd_tui)

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return int(args.fn(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # `looper show 9 | head` closes the pipe early
        sys.stdout.close()
        return 0
    except EOFError:  # init read from a closed stdin
        print("\nno input: run init in a terminal, or pass --repo and --model", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
