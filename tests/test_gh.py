import pytest

from Looper.gh import GH, GHError


def test_remove_label_ignores_missing_label_but_raises_real_failures(monkeypatch):
    gh = GH("o/n")

    def missing(args, **_kw):
        raise GHError(args, 1, "could not remove label: 'agent:needs-human' not found")

    monkeypatch.setattr(gh, "_run", missing)
    gh.remove_label(1, "agent:needs-human")
    gh.remove_pr_label(2, "agent:needs-human")

    def forbidden(args, **_kw):
        raise GHError(args, 1, "HTTP 403: Resource not accessible by integration")

    monkeypatch.setattr(gh, "_run", forbidden)
    with pytest.raises(GHError):
        gh.remove_pr_label(2, "agent:needs-human")
