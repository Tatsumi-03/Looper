from pathlib import Path

from Looper.config import GreptileCfg
from Looper.greptile import ReviewResult, find_review, parse_score


def test_parse_score_variants():
    assert parse_score("## Confidence score: 4/5") == 4
    assert parse_score("### Confidence Score\n\n5/5 — production ready") == 5
    assert parse_score("Confidence score: **3/5** [View in Greptile](http://x)") == 3
    assert parse_score("| Confidence | 2/5 |") == 2
    assert parse_score("changed 3/5 files\n\nConfidence score: 0/5") == 0
    assert parse_score("Score: 5 / 5") == 5
    assert parse_score("no verdict here") is None
    assert parse_score("") is None


def test_parse_score_prefers_confidence_line():
    body = "Reviewed 4/5 files in this PR.\n\n## Confidence score: 2/5\n"
    assert parse_score(body) == 2


class FakeGH:
    def __init__(self, comments=(), reviews=(), inline=()):
        self._comments, self._reviews, self._inline = list(comments), list(reviews), list(inline)

    def pr_issue_comments(self, _pr):
        return self._comments

    def pr_reviews(self, _pr):
        return self._reviews

    def pr_inline_comments(self, _pr):
        return self._inline


CFG = GreptileCfg(bot_logins=["greptile-apps[bot]"])


def test_find_review_ignores_humans_and_stale_reviews():
    gh = FakeGH(comments=[
        {"user": {"login": "someone"}, "body": "Confidence score: 5/5",
         "updated_at": "2026-01-02T00:00:00Z"},
        {"user": {"login": "greptile-apps[bot]"}, "body": "Confidence score: 1/5",
         "updated_at": "2026-01-01T00:00:00Z"},  # predates the push
    ])
    assert find_review(gh, 1, CFG, head_sha="deadbee" * 6, pushed_at="2026-01-01T12:00:00+00:00") is None


def test_find_review_accepts_fresh_by_timestamp_and_sha():
    by_time = FakeGH(comments=[{"user": {"login": "greptile-apps[bot]"},
                                "body": "## Confidence score: 3/5",
                                "updated_at": "2026-01-02T00:00:00Z", "html_url": "u"}])
    r = find_review(by_time, 1, CFG, head_sha="abc1234", pushed_at="2026-01-01T00:00:00+00:00")
    assert isinstance(r, ReviewResult) and r.score == 3 and r.source == "comment"

    by_sha = FakeGH(reviews=[{"user": {"login": "greptile-apps[bot]"},
                              "body": "Confidence score: 5/5 for abc1234def",
                              "submitted_at": "2020-01-01T00:00:00Z"}])
    r = find_review(by_sha, 1, CFG, head_sha="abc1234def", pushed_at="2026-01-01T00:00:00+00:00")
    assert r and r.score == 5 and r.source == "review"


def test_find_review_collects_inline_comments():
    gh = FakeGH(
        comments=[{"user": {"login": "greptile-apps[bot]"}, "body": "Confidence score: 2/5",
                   "updated_at": "2026-01-02T00:00:00Z"}],
        inline=[{"user": {"login": "greptile-apps[bot]"}, "path": "a.py", "line": 12,
                 "body": "null deref", "diff_hunk": "@@ -1 +1 @@", "updated_at": "2026-01-02T00:01:00Z"},
                {"user": {"login": "human"}, "path": "b.py", "line": 1, "body": "nit"}],
    )
    r = find_review(gh, 1, CFG, head_sha="x", pushed_at="2026-01-01T00:00:00+00:00")
    assert r and len(r.inline) == 1
    assert "a.py:12" in r.render() and "null deref" in r.render()


def test_parses_a_real_greptile_summary():
    """Captured from greptile-apps[bot] on a live PR: the score sits inside an <h2>
    that also carries the retrigger badge markup, all on one line."""
    body = (Path(__file__).parent / "fixtures" / "greptile_summary_5of5.md").read_text()
    assert parse_score(body) == 5

    gh = FakeGH(comments=[{"user": {"login": "greptile-apps[bot]"}, "body": body,
                           "updated_at": "2026-09-17T14:45:35Z", "html_url": "u"}])
    sha = "c67ba973e8e4ec9925c4f94a011d3f8d6164f432"  # the "Last reviewed commit" link
    review = find_review(gh, 4, CFG, head_sha=sha, pushed_at="2030-01-01T00:00:00+00:00")
    assert review and review.score == 5, "the reviewed-commit link must satisfy the freshness gate"
