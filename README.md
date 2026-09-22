# Looper

A daemon that turns open GitHub issues into merge-ready pull requests, watched from a TUI.

For each open issue it spawns a Claude Code agent in its own git worktree, opens a PR,
waits for [Greptile](https://greptile.com)'s review, and feeds the findings back to the
agent — round after round — until Greptile's confidence score hits **5/5**. Then it
labels the PR `agent:ready` and stops. A human merges.

Several agents run at once, one worktree and branch each.

```
issue ──► claim ──► worktree ──► agent solves ──► push ──► PR
                                                            │
                          ┌─────────────────────────────────┘
                          ▼
                    Greptile scores N/5 ──► 5/5? ──► agent:ready (human merges)
                          ▲                   │
                          └── agent revises ◄─┘   (up to max_iterations)
```

## Requirements

- Python 3.11+ (no third-party dependencies)
- [`gh`](https://cli.github.com/) authenticated with `repo` scope — Looper does *all*
  GitHub work through it
- [`claude`](https://claude.com/claude-code) on `PATH` and logged in
- The Greptile GitHub App installed on the target repo

## Install

Everything above under [Requirements](#requirements) has to be in place first —
Looper itself pulls in nothing else.

**1. Clone it**

```bash
git clone https://github.com/<you>/Looper.git
cd Looper
```

**2. Check your Python**

```bash
python3 --version        # needs 3.11 or newer
```

**3. Pick how you want to run it**

Straight from the checkout, no install:

```bash
./looper --help
```

Or put a `looper` command on your `PATH` (a venv keeps it off your system Python):

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/looper --help
```

**4. Point it at a repo**

```bash
./looper init --repo owner/name        # writes looper.toml
$EDITOR looper.toml                    # model, budgets, concurrency, skip labels
```

**5. Check your setup**

```bash
./looper doctor                        # verifies config, gh, git, claude, repo access
```

Every line should say `ok`. A `FAIL` here tells you exactly which requirement is
missing before anything touches a real repo.

## Quick start

```bash
# rehearsal: no push, no PR, no comments
./looper once 123 --dry-run

# exercise the whole revise loop without waiting on Greptile
./looper once 123 --fake-review 3,4,5

# the real thing
./looper once 123                      # one issue, foreground
./looper daemon                        # watch every open issue, headless
./looper tui                           # same, with a live dashboard
```

## Commands

| command | what it does |
|---|---|
| `daemon` | poll open issues and work them continuously |
| `tui` | the daemon plus a live dashboard: task table, agent controls, scrollable log |
| `once <issue>` | drive a single issue end to end in the foreground |
| `status` | table of every task: state, PR, score, cost |
| `show <issue>` | full detail plus the event log for one issue |
| `retry <issue> [--restart]` | un-park a task (`--restart` forgets the PR and session) |
| `abandon <issue> [--clean]` | park a task by hand |
| `clean [--all]` | delete worktrees of finished tasks |
| `doctor` | verify config, tooling and GitHub access |
| `init --repo owner/name` | write a starter `looper.toml` |

## How it works

**State lives in SQLite** (`var/looper.db`), not in memory. Every transition is recorded,
so a daemon restart resumes each task from where it stopped instead of re-solving the issue
or opening a second PR.

```
PENDING → CLAIMED → WORKTREE → SOLVING → PUSHED → PR_OPEN
        → AWAITING_REVIEW → SCORED ─┬─ 5/5 ─→ READY_FOR_HUMAN ─→ MERGED
                                    └─ <5 ──→ REVISING → PUSHED → …
                                                      → PARKED (needs a human)
```

`MERGED` is set by the poll loop once a human lands the PR — merging closes the issue,
so it drops out of the open-issue listing and the PR has to be asked about directly.
The TUI hides merged tasks; `looper status` keeps them as history.

**The agent never talks to GitHub.** It edits files and commits inside its worktree; that is
all. Looper owns `git push`, the PR, and every comment and label. `gh` and `git push`
are on the agent's denylist, and pushes are refused for any branch outside `agent/issue-*`.

**But the agent writes the PR description.** It ends its message with a `<pr-description>`
block and Looper makes that the PR body, rewriting it on every revision round. This
matters more than it sounds: repos whose contribution rules demand a test plan or before/after
evidence *in the description* are scored on it, and an agent with no channel to the body can
never get past 4/5.

**Reading the score.** Greptile posts its summary and `Confidence score: N/5` as a top-level
PR comment, sometimes as a formal review, and its findings as inline comments — Looper
reads all three. A review only counts for the current round if it names the current head SHA
or post-dates the push, so a stale verdict is never mistaken for a fresh one. If nothing
arrives within `review_timeout_sec`, Looper comments `@greptileai review` to re-trigger,
up to `max_retriggers` times.

**Giving up is a state, not a crash.** Iteration cap, per-issue budget, a silent reviewer, an
agent that declines the issue (`HARNESS_ABORT:`) or produces no commits — each parks the task:
the PR stays open, gets `agent:needs-human`, and a comment explains what happened. Nothing is
closed or deleted automatically.

## Configuration

See `src/Looper/looper.toml.example` for every option. The ones worth knowing:

| key | default | why you'd change it |
|---|---|---|
| `loop.max_concurrent_agents` | 3 | how many agents run at once |
| `loop.max_iterations` | 5 | review rounds before parking |
| `loop.max_open_prs` | 5 | back-pressure on the repo |
| `loop.skip_labels` | `no-agent, wontfix, blocked, question` | issues the daemon ignores |
| `agent.max_budget_usd` | 5.0 | ceiling per agent run |
| `safety.max_cost_per_issue_usd` | 25.0 | ceiling per issue, across all rounds |
| `greptile.target_score` | 5 | lower it to 4 if 5/5 proves unreachable |
| `greptile.fake_scores` | `[]` | canned scores for testing the loop |
| `safety.dry_run` | false | everything except push, PR and comments |
| `safety.auto_merge` | false | deliberately unused: a human merges |

## Safety

- no auto-merge — Looper stops at `agent:ready`
- pushes are guarded by branch prefix; force-push and base-branch pushes are refused
- the agent's tool allowlist excludes `gh`, `git push`, `git remote`, `sudo`, `rm -rf`
- `--permission-prompts none`: an unexpected tool call is denied, not left hanging
- per-run and per-issue dollar ceilings
- one daemon per checkout (`var/daemon.lock`), one worker per issue (`var/locks/`)
- `dry_run` and `--fake-review` let you rehearse the whole loop before it can touch anything

OS-level sandboxing (Claude Code's `--sandbox`, via bubblewrap on Linux) is a planned
follow-up — today's guardrails are process-level (tool denylist + branch-prefix push guard),
not a kernel-enforced boundary.

## Layout

```
src/Looper/
  cli.py           commands
  orchestrator.py  the state machine
  state.py         SQLite store
  gh.py            every `gh` call
  worktree.py      base clone, per-issue worktrees, guarded push
  agent.py         Claude Code headless driver
  greptile.py      score parsing, freshness, re-trigger
  tui.py           curses dashboard; embeds the daemon in a background thread
  prompts/         solve_issue.md, address_review.md
var/               db, logs, worktrees, clones  (gitignored)
```

Agent transcripts land in `var/logs/issue-<n>/` — the prompt, the full `stream-json`
transcript, and the review text for every round.

## TUI

`looper tui` runs the daemon itself, so it replaces `looper daemon` rather than
watching one. The task table is on top, a scrollable daemon log below it.

| key | what it does |
|---|---|
| `j` / `k` | move down / up the task list |
| `a` | start an agent on the selected issue now (un-parks it first if needed) |
| `r` | reset a task's state and let the next poll cycle pick it up |
| `x` | park a task by hand |
| `c` | delete a finished task's worktree |
| `PgUp` / `PgDn` | scroll the log; `PgDn` back to the bottom resumes following |
| `q` | quit |

## Tests

```bash
python -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests -q
```

`tests/test_flow.py` runs the whole state machine against real git worktrees and a real
local origin, with GitHub and Claude stubbed out — no network, no API spend.
