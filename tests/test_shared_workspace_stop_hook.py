"""The OpenHands shared-workspace hook against a real per-run Git worktree.

This is the cross-layer check for the runtime contract the audit identified:
OpenHands runs the policy module as a synchronous ``PostToolUse``/``Stop``
command, and the Factory still repairs and re-validates as defense in depth. The
hook is invoked exactly as OpenHands would — as a subprocess reading the event
JSON on stdin — so the exit-code contract is exercised for real.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from factory.domain.enums import RunStatus, ValidationOutcome
from factory.integrations.workspace.git import GitWorktreeWorkspaceProvisioner
from factory.integrations.workspace.revision import GitWorkspaceRevisionInspector
from factory.orchestration.tracking import RunTrackingService
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_workspace import FakeQualityGateRunner, specs
from tests.test_validated_revision import _repos, _setup

_SRC = Path(__file__).resolve().parent.parent / "src"


def _run_hook(working_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    return subprocess.run(
        [sys.executable, "-m", "factory.integrations.workspace.shared_policy"],
        input=f'{{"working_dir": "{working_dir}"}}',
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_post_tool_use_hook_normalizes_a_new_file_owner_side(tmp_path: Path) -> None:
    source, _, task, run = _setup(tmp_path)
    assert run.workspace is not None
    root = Path(run.workspace.path)
    provisioner = GitWorktreeWorkspaceProvisioner(str(source))
    provisioner.prepare(task, run.workspace)
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")

    # Simulate file_editor/create: a brand-new file left at 0600.
    created = root / "module.py"
    created.write_text("VALUE = 1\n", encoding="utf-8")
    created.chmod(0o600)
    secret = root / ".env"
    secret.write_text("TEST_ONLY=1\n", encoding="utf-8")
    secret.chmod(0o644)  # ignored, so the hook must force it back to 0600

    completed = _run_hook(root)

    assert completed.returncode == 0
    assert _mode(created) == 0o660
    assert created.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _mode(secret) == 0o600


def test_stop_hook_blocks_and_the_factory_still_fails_closed(tmp_path: Path) -> None:
    source, _, task, run = _setup(tmp_path)
    assert run.workspace is not None
    workspace = run.workspace
    root = Path(workspace.path)
    provisioner = GitWorktreeWorkspaceProvisioner(str(source))
    provisioner.prepare(task, workspace)
    # An unsafe node the policy refuses: a special file cannot be a shared
    # regular file, so owner-side normalization cannot make it compliant.
    os.mkfifo(root / "unsafe_pipe")

    hook = _run_hook(root)
    # The Stop hook blocks completion: OpenHands must not report success.
    assert hook.returncode == 2
    assert str(tmp_path) not in hook.stderr

    # And even if a run were reported successful anyway, the Factory's own
    # post-agent repair fails closed: it binds no revision and the run is green
    # on nothing that could be published.
    tasks, runs, _ = _repos(str(tmp_path / "factory.db"))
    runner = FakeQualityGateRunner()
    result = RunTrackingService(
        tasks,
        runs,
        gate_specs=specs("tests"),
        gate_runner=runner,
        provisioner=provisioner,
        revision_inspector=GitWorkspaceRevisionInspector(),
    ).refresh(run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED))

    assert result.outcome is ValidationOutcome.GATES_FAILED
    assert result.run.validated_revision is None
    assert runner.calls == []
