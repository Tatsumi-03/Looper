"""Reading Greptile's verdict off a PR.

Greptile posts its summary and ``Confidence score: N/5`` as a top-level PR
conversation comment (an *issue* comment), and sometimes as a formal review, so
both endpoints are polled. Line-level findings arrive as inline review comments.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .config import GreptileCfg
from .gh import GH

log = logging.getLogger("looper.greptile")

SCORE_IN_LINE = re.compile(r"([0-5])\s*/\s*5")
CONFIDENCE_LINE = re.compile(r"confidence", re.I)
SCORE_LINE = re.compile(r"\bscore\b", re.I)
MAX_HUNK = 800


@dataclass
class InlineComment:
    path: str
    line: int | None
    body: str
    diff_hunk: str = ""
    url: str = ""

    def render(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        hunk = f"\n```diff\n{self.diff_hunk[:MAX_HUNK]}\n```" if self.diff_hunk else ""
        return f"### {loc}{hunk}\n{self.body.strip()}"


@dataclass
class ReviewResult:
    score: int | None
    summary: str
    updated_at: str
    source: str
    url: str = ""
    inline: list[InlineComment] = field(default_factory=list)

    def render(self) -> str:
        parts = [f"## Greptile review — confidence score {self.score}/5", self.summary.strip()]
        if self.inline:
            parts.append(f"## Inline findings ({len(self.inline)})")
            parts.extend(c.render() for c in self.inline)
        return "\n\n".join(parts)


def parse_score(body: str) -> int | None:
    """Pull ``N/5`` out of a Greptile comment body, preferring the confidence line."""
    if not body:
        return None
    lines = body.splitlines()
    for matcher in (CONFIDENCE_LINE, SCORE_LINE):
        for line in lines:
            if matcher.search(line) and (m := SCORE_IN_LINE.search(line)):
                return int(m.group(1))
    # score rendered on the line *after* its heading (common in tables/badges)
    for i, line in enumerate(lines):
        if CONFIDENCE_LINE.search(line):
            for nxt in lines[i + 1:i + 3]:
                if m := SCORE_IN_LINE.search(nxt):
                    return int(m.group(1))
    return None


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_bot(author: dict[str, Any] | None, logins: list[str]) -> bool:
    login = (author or {}).get("login", "")
    return any(login.lower() == b.lower() for b in logins)


def _fresh(body: str, updated_at: str | None, head_sha: str | None,
           pushed_at: str | None) -> bool:
    """A review counts for this round if it names the current head, or post-dates the push."""
    if head_sha and (head_sha in body or head_sha[:7] in body):
        return True
    a, b = _ts(updated_at), _ts(pushed_at)
    return bool(a and b and a > b)


def collect_inline(gh: GH, pr: int, cfg: GreptileCfg, pushed_at: str | None) -> list[InlineComment]:
    raw = gh.pr_inline_comments(pr)
    bot = [c for c in raw if _is_bot(c.get("user"), cfg.bot_logins)]
    recent = [c for c in bot if _fresh(c.get("body", ""), c.get("updated_at"), None, pushed_at)]
    chosen = recent or bot
    return [
        InlineComment(
            path=c.get("path", "?"),
            line=c.get("line") or c.get("original_line"),
            body=c.get("body", ""),
            diff_hunk=c.get("diff_hunk", ""),
            url=c.get("html_url", ""),
        )
        for c in chosen
    ]


def find_review(gh: GH, pr: int, cfg: GreptileCfg, *, head_sha: str | None,
                pushed_at: str | None) -> ReviewResult | None:
    """Most recently updated Greptile verdict for the current head, or None."""
    candidates: list[tuple[str, str, str, str]] = []  # (updated_at, body, url, source)
    for c in gh.pr_issue_comments(pr):
        if _is_bot(c.get("user"), cfg.bot_logins):
            candidates.append((c.get("updated_at") or c.get("created_at", ""), c.get("body", ""),
                               c.get("html_url", ""), "comment"))
    for r in gh.pr_reviews(pr):
        if _is_bot(r.get("user"), cfg.bot_logins) and r.get("body"):
            candidates.append((r.get("submitted_at", ""), r.get("body", ""),
                               r.get("html_url", ""), "review"))
    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0], reverse=True)
    for updated_at, body, url, source in candidates:
        if not _fresh(body, updated_at, head_sha, pushed_at):
            continue
        return ReviewResult(
            score=parse_score(body),
            summary=body,
            updated_at=updated_at,
            source=source,
            url=url,
            inline=collect_inline(gh, pr, cfg, pushed_at),
        )
    return None


def fake_review(score: int) -> ReviewResult:
    return ReviewResult(
        score=score,
        summary=f"[fake review] Confidence score: {score}/5 — injected by greptile.fake_scores.",
        updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        source="fake",
    )


async def wait_for_review(
    gh: GH,
    cfg: GreptileCfg,
    pr: int,
    *,
    head_sha: str | None,
    pushed_at: str | None,
    retriggers_used: int = 0,
    on_retrigger: Callable[[int], None] | None = None,
    on_wait: Callable[[int], None] | None = None,
) -> ReviewResult | None:
    """Poll until a scored review for this head appears. Re-triggers Greptile on timeout."""
    deadline_budget = cfg.review_timeout_sec
    waited = 0
    retriggers = retriggers_used
    while True:
        review = await asyncio.to_thread(
            find_review, gh, pr, cfg, head_sha=head_sha, pushed_at=pushed_at
        )
        if review and review.score is not None:
            log.info("PR #%s scored %s/5 (%s)", pr, review.score, review.source)
            return review
        if review:
            log.info("PR #%s: fresh Greptile comment with no parsable score yet", pr)
        if waited >= deadline_budget:
            if retriggers >= cfg.max_retriggers:
                log.warning("PR #%s: no Greptile score after %s retriggers — giving up", pr, retriggers)
                return None
            retriggers += 1
            log.warning("PR #%s: no review after %ss — re-triggering (%s/%s)",
                        pr, waited, retriggers, cfg.max_retriggers)
            await asyncio.to_thread(gh.pr_comment, pr, cfg.retrigger_body)
            if on_retrigger:
                on_retrigger(retriggers)
            waited = 0
        await asyncio.sleep(cfg.poll_interval_sec)
        waited += cfg.poll_interval_sec
        if waited % 60 < cfg.poll_interval_sec:
            log.info("PR #%s: waiting on Greptile — %dm elapsed of %dm before re-trigger",
                     pr, waited // 60, deadline_budget // 60)
        if on_wait:
            on_wait(waited)
