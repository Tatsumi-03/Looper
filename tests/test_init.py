import argparse

import pytest

from Looper import cli, config
from Looper.config import ConfigError
from Looper.gh import GH, GHError

REPOS = {  # a fake GitHub: canonical name, default branch, branches
    "tatsumi-03/looper": ("Tatsumi-03/Looper", "main", {"main", "dev"}),
    "acme/api": ("acme/api", "trunk", {"trunk"}),
}


@pytest.fixture
def github(monkeypatch):
    """Fake GitHub for init; returns the (repo, label) pairs it was asked to create."""
    labels = []

    def api(self, path):
        parts = path.split("/")  # repos/<owner>/<name>[/branches/<branch>]
        name, default, branches = REPOS.get(f"{parts[1]}/{parts[2]}".lower(), (None, None, set()))
        if name and len(parts) == 3:
            return {"full_name": name, "default_branch": default}
        if name and parts[3:4] == ["branches"] and parts[4] in branches:
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
    return argparse.Namespace(**{"config": str(target), "repo": None, "model": None,
                                 "base_branch": None, **flags})


def repos(target):
    return [(r.slug, r.base_branch, r.model) for r in config.load(target, root=target.parent).repos]


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
    assert repos(target) == [("Tatsumi-03/Looper", "main", "sonnet")]
    assert github == [("Tatsumi-03/Looper", "agent")]


def test_init_bad_flag_fails_without_asking_or_writing(tmp_path, monkeypatch, github):
    monkeypatch.setattr("builtins.input", lambda _q: pytest.fail("init asked despite --repo"))
    target = tmp_path / "looper.toml"

    assert cli.cmd_init(init_args(target, repo="nope/missing")) == 1
    assert not target.exists()


def test_init_adds_repos_and_rerun_updates_in_place(tmp_path, monkeypatch, github):
    target = tmp_path / "looper.toml"
    answering(monkeypatch, "Tatsumi-03/Looper", "dev", "opus")
    cli.cmd_init(init_args(target))
    # a hand edit elsewhere in the file must survive every later init
    target.write_text(target.read_text().replace("max_iterations = 5", "max_iterations = 9  # mine"))

    answering(monkeypatch, "acme/api", "", "")  # second repo: its own default branch
    cli.cmd_init(init_args(target))
    answering(monkeypatch, "tatsumi-03/looper", "", "")  # rerun: Enter keeps dev and opus
    cli.cmd_init(init_args(target))

    assert repos(target) == [("Tatsumi-03/Looper", "dev", "opus"), ("acme/api", "trunk", "sonnet")]
    assert "max_iterations = 9  # mine" in target.read_text()


def test_commands_need_repo_once_there_are_several(tmp_path, monkeypatch, github):
    target = tmp_path / "looper.toml"
    for answers in (("Tatsumi-03/Looper", "", "opus"), ("acme/api", "", "haiku")):
        answering(monkeypatch, *answers)
        cli.cmd_init(init_args(target))
    args = argparse.Namespace(config=str(target), repo=None)

    with pytest.raises(ConfigError, match="pick one with --repo"):
        cli._load(args)
    args.repo = "ACME/api"
    cfg = cli._load(args)
    assert (cfg.repo.slug, cfg.agent.model) == ("acme/api", "haiku")  # the entry's model wins
    assert cfg.var_dir.name == "acme__api"


def test_old_single_repo_section_says_how_to_fix_it(tmp_path):
    target = tmp_path / "looper.toml"
    target.write_text('[repo]\nslug = "acme/api"\n')
    with pytest.raises(ConfigError, match=r"\[repo\] is now \[\[repos\]\]"):
        config.load(target, root=tmp_path)
