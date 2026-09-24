import argparse

import pytest

from Looper import cli, config
from Looper.gh import GH, GHError

REPO = {"full_name": "Tatsumi-03/Looper", "default_branch": "main"}
BRANCHES = {"main", "dev"}


@pytest.fixture
def github(monkeypatch):
    """A fake GitHub with one repo; records labels created on it."""
    labels = []

    def api(self, path):
        if path.lower() == "repos/tatsumi-03/looper":
            return REPO
        if path.startswith("repos/Tatsumi-03/Looper/branches/") and path.rsplit("/", 1)[1] in BRANCHES:
            return {}
        raise GHError(["api", path], 1, "gh: Not Found (HTTP 404)")

    monkeypatch.setattr(GH, "api", api)
    monkeypatch.setattr(GH, "ensure_label", lambda self, label: labels.append((self.slug, label)) or True)
    monkeypatch.setattr(cli, "_cwd_repo", lambda: "")
    monkeypatch.setattr(cli, "cmd_doctor", lambda _a: 0)
    return labels


def answering(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _q: next(it))


def init_args(target, **flags):
    return argparse.Namespace(**{"config": str(target), "force": False, "repo": None,
                                 "model": None, "base_branch": None, **flags})


@pytest.mark.parametrize("typed", [
    "acme/api", " acme/api/ ", "https://github.com/acme/api", "https://github.com/acme/api.git",
    "git@github.com:acme/api.git", "github.com/acme/api",
])
def test_repo_slug_accepts_what_people_paste(typed):
    assert cli.repo_slug(typed) == "acme/api"


def test_init_reasks_each_bad_answer_and_writes_what_github_reports(tmp_path, monkeypatch, github):
    answering(monkeypatch,
              "nope/missing",                              # not on GitHub -> asked again
              "https://github.com/tatsumi-03/looper.git",  # pasted URL, wrong case
              "feature-x", "",                             # no such branch, then GitHub's default
              "sonet", "")                                 # typo, then the default model
    target = tmp_path / "looper.toml"

    assert cli.cmd_init(init_args(target)) == 0
    cfg = config.load(target, root=tmp_path)
    assert (cfg.repo.slug, cfg.repo.base_branch, cfg.agent.model) == ("Tatsumi-03/Looper", "main", "sonnet")
    assert github == [("Tatsumi-03/Looper", "agent")]


def test_init_bad_flag_fails_without_asking_or_writing(tmp_path, monkeypatch, github):
    monkeypatch.setattr("builtins.input", lambda _q: pytest.fail("init asked despite --repo"))
    target = tmp_path / "looper.toml"

    assert cli.cmd_init(init_args(target, repo="nope/missing")) == 1
    assert not target.exists()


def test_init_rerun_offers_the_current_settings(tmp_path, monkeypatch, github):
    target = tmp_path / "looper.toml"
    answering(monkeypatch, "Tatsumi-03/Looper", "dev", "opus")
    cli.cmd_init(init_args(target))

    answering(monkeypatch, "y", "", "", "")  # overwrite, then Enter through every question
    assert cli.cmd_init(init_args(target)) == 0
    cfg = config.load(target, root=tmp_path)
    assert (cfg.repo.slug, cfg.repo.base_branch, cfg.agent.model) == ("Tatsumi-03/Looper", "dev", "opus")
