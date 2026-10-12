"""A new kanban worktree task branches from the remote default branch.

A card created with ``--workspace worktree:<repo>/.worktrees/<slug> --branch <new>``
used to get ``git worktree add -b <new> <path> HEAD``: whatever the repo checkout had
checked out, with no fetch. A main checkout parked on a months-old feature branch
handed every fix lane that branch's history, so workers implemented fixes against
code no longer on main. ``_ensure_git_worktree`` now bases a NEW task branch on
``worktree_ops._resolve_worktree_base(..., prefer_default_branch=True)`` (fetched
``origin/HEAD``); an EXISTING task branch is reattached at its own tip, untouched.

Real git throughout: a bare "origin", a seed that pushes to it, and a clone whose
checkout sits on a feature branch that tracks ``origin/feature`` and lags main.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw

_LOGGER = "hermes_cli.kanban_db"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()


def _commit(repo: Path, name: str, msg: str) -> str:
    (repo / name).write_text(msg + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _head(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD")


def _age_fetch_head(clone: Path) -> None:
    """Push FETCH_HEAD outside the resolver's freshness window so it fetches again."""
    fetch_head = clone / ".git" / "FETCH_HEAD"
    old = time.time() - 3600
    os.utime(fetch_head, (old, old))


@pytest.fixture
def repos(tmp_path):
    """-> dict(clone, seed, origin, main_tip, feature_tip).

    The clone's checkout is ``feature`` (upstream ``origin/feature``), one commit off an
    OLD main; ``origin/main`` is two commits ahead of that old main and already fetched.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _commit(seed, "README.md", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git(clone, "checkout", "-q", "-b", "feature")
    feature_tip = _commit(clone, "feature.txt", "feature work")
    _git(clone, "push", "-q", "-u", "origin", "feature")

    _commit(seed, "main1.txt", "main moves on 1")
    main_tip = _commit(seed, "main2.txt", "main moves on 2")
    _git(seed, "push", "-q", "origin", "main")
    _git(clone, "fetch", "-q", "origin")

    assert _git(clone, "rev-parse", "origin/main") == main_tip
    assert _git(clone, "symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/main"
    assert _head(clone) == feature_tip != main_tip
    return {"clone": clone, "seed": seed, "origin": origin,
            "main_tip": main_tip, "feature_tip": feature_tip}


def test_new_task_branch_starts_at_origin_main_not_checkout_head(repos, caplog):
    clone = repos["clone"]
    target = clone / ".worktrees" / "t_new"
    caplog.set_level(logging.INFO, logger=_LOGGER)

    kbw._ensure_git_worktree(clone, target, "fix/new-lane")

    assert _head(target) == repos["main_tip"]
    assert _head(target) != repos["feature_tip"]
    assert not (target / "feature.txt").exists()
    # The task branch must not track main: a `git push`/`pull` in the worker would hit main.
    upstream = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--abbrev-ref", "fix/new-lane@{upstream}"],
        capture_output=True, text=True, check=False,
    )
    assert upstream.returncode != 0, upstream.stdout
    assert any(r.levelno == logging.INFO and "origin/main" in r.getMessage()
               for r in caplog.records)


def test_origin_advanced_since_last_fetch_is_fetched(repos):
    clone, seed = repos["clone"], repos["seed"]
    _age_fetch_head(clone)
    new_tip = _commit(seed, "main3.txt", "landed after the last fetch")
    _git(seed, "push", "-q", "origin", "main")
    target = clone / ".worktrees" / "t_fetch"

    kbw._ensure_git_worktree(clone, target, "fix/fetched")

    assert _head(target) == new_tip
    assert (target / "main3.txt").exists()


def test_fetch_failure_uses_cached_origin_main_not_head(repos, tmp_path):
    clone, seed = repos["clone"], repos["seed"]
    _commit(seed, "main3.txt", "unreachable commit")
    _git(seed, "push", "-q", "origin", "main")
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    _age_fetch_head(clone)
    target = clone / ".worktrees" / "t_cached"

    kbw._ensure_git_worktree(clone, target, "fix/cached")

    assert _head(target) == repos["main_tip"]


def test_no_remote_falls_back_to_head_with_warning(tmp_path, caplog):
    repo = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _commit(repo, "README.md", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    local_head = _commit(repo, "feature.txt", "feature work")
    target = repo / ".worktrees" / "t_solo"
    caplog.set_level(logging.INFO, logger=_LOGGER)

    kbw._ensure_git_worktree(repo, target, "fix/solo")

    assert _head(target) == local_head
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("HEAD" in m and "fix/solo" in m for m in warnings), warnings


def test_existing_branch_reattaches_at_its_own_tip(repos):
    clone = repos["clone"]
    _git(clone, "branch", "fix/existing", repos["feature_tip"])
    target = clone / ".worktrees" / "t_existing"

    kbw._ensure_git_worktree(clone, target, "fix/existing")

    assert _head(target) == repos["feature_tip"]
    assert _git(clone, "rev-parse", "fix/existing") == repos["feature_tip"]


def test_worktree_sync_false_keeps_local_head(repos):
    hermes_home = Path(os.environ["HERMES_HOME"])
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text("worktree_sync: false\n", encoding="utf-8")
    clone = repos["clone"]
    target = clone / ".worktrees" / "t_nosync"

    kbw._ensure_git_worktree(clone, target, "fix/nosync")

    assert _head(target) == repos["feature_tip"]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize("workspace", ["repo_root", "explicit_path"])
def test_task_workspace_resolution_uses_default_branch_base(repos, kanban_home, workspace):
    clone = repos["clone"]
    path = clone if workspace == "repo_root" else clone / ".worktrees" / "named-slot"
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fix it", workspace_kind="worktree",
                             workspace_path=str(path), branch_name="fix/card")
        task = kb.get_task(conn, tid)
    assert task is not None

    resolved, branch = kbw._resolve_worktree_workspace(task)

    assert branch == "fix/card"
    assert _head(resolved) == repos["main_tip"]
