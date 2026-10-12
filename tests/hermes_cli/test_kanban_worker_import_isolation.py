"""A module-form kanban worker never imports code from its task workspace.

The dispatcher spawns ``sys.executable -m hermes_cli.main ...`` with ``cwd=<workspace>``.
``-m`` puts the cwd FIRST on ``sys.path``, so a worker whose workspace is a hermes-agent
checkout (a worktree lane fixing Hermes itself) imported the TARGET tree instead of the
install that spawned it: tracebacks under ``.worktrees/<slug>/agent/...``, crashing inside
the runtime's own dependencies before any work. The module-form argv now carries ``-P``
(Python 3.11+, the ``requires-python`` floor) so the cwd is never prepended; the install
root pinned on ``PYTHONPATH`` wins.

Real subprocess spawn through ``_default_spawn``: the workspace holds decoy ``hermes_cli``
and ``agent`` packages that write a marker on import.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

_DECOY = (
    "from pathlib import Path\n"
    "Path({marker!r}).write_text(__name__ + ' imported from the task workspace', encoding='utf-8')\n"
)


@pytest.fixture
def decoy_workspace(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.delenv("HERMES_BIN", raising=False)

    workspace = tmp_path / "ws"
    marker = tmp_path / "decoy-imported.txt"
    for package in ("hermes_cli", "agent"):
        (workspace / package).mkdir(parents=True)
        (workspace / package / "__init__.py").write_text(_DECOY.format(marker=str(marker)),
                                                         encoding="utf-8")
    (workspace / "hermes_cli" / "main.py").write_text(_DECOY.format(marker=str(marker)),
                                                     encoding="utf-8")
    return workspace, marker


def test_module_worker_imports_the_installed_package_not_the_workspace(decoy_workspace, monkeypatch):
    workspace, marker = decoy_workspace
    # The real resolver and spawn path; only the CLI verb is swapped for a side-effect-free one.
    monkeypatch.setattr(kbd, "_worker_argv",
                        lambda task, profile, home: [*kbd._resolve_hermes_argv(), "--version"])
    assert kbd._resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]
    task = kb.Task(
        id="t_decoy", title="x", body=None, assignee="coder", status="ready",
        priority=0, created_by=None, created_at=0, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=str(workspace), claim_lock=None,
        claim_expires=None, tenant=None, branch_name=None,
    )

    pid = kbd._default_spawn(task, str(workspace))
    assert pid is not None
    rc = psutil.Process(pid).wait(timeout=120)

    log = (kb.worker_logs_dir() / "t_decoy.log").read_text(encoding="utf-8", errors="replace")
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert rc == 0, log
