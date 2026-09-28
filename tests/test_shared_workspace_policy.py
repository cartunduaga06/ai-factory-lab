"""Cross-UID shared-workspace permission policy.

These tests exercise the one policy module both the Factory-side repair and the
OpenHands owner-side normalizer call. They build disposable local Git
repositories under ``tmp_path`` — never a real product repository or network —
and assert real filesystem modes.
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from factory.integrations.workspace.shared_policy import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_EXECUTABLE_MODE,
    PRIVATE_FILE_MODE,
    SHARED_DIRECTORY_MODE,
    SHARED_EXECUTABLE_MODE,
    SHARED_FILE_MODE,
    SharedWorkspacePolicyError,
    classify_ignored_paths,
    main,
    normalize_workspace,
    normalize_workspace_path,
    target_directory_mode,
    target_file_mode,
)

_GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, env=_GIT_ENV
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    return root


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _deny_chmod(monkeypatch: pytest.MonkeyPatch, nodes: list[Path]) -> list[tuple[int, int]]:
    """Simulate foreign-owned nodes by denying chmod on selected inodes."""
    foreign = {(node.stat().st_dev, node.stat().st_ino) for node in nodes}
    attempted: list[tuple[int, int]] = []
    original = os.fchmod

    def chmod(fd: int, mode: int) -> None:
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        if identity in foreign:
            attempted.append(identity)
            raise PermissionError("foreign inode cannot be chmodded")
        original(fd, mode)

    monkeypatch.setattr(os, "fchmod", chmod)
    return attempted


# -- pure policy -----------------------------------------------------------


@pytest.mark.parametrize(
    ("ignored", "executable", "expected"),
    [
        (False, False, SHARED_FILE_MODE),
        (False, True, SHARED_EXECUTABLE_MODE),
        (True, False, PRIVATE_FILE_MODE),
        (True, True, PRIVATE_EXECUTABLE_MODE),
    ],
)
def test_target_file_modes(ignored: bool, executable: bool, expected: int) -> None:
    assert target_file_mode(ignored=ignored, executable=executable) == expected


@pytest.mark.parametrize(
    ("ignored", "expected"),
    [(False, SHARED_DIRECTORY_MODE), (True, PRIVATE_DIRECTORY_MODE)],
)
def test_target_directory_modes(ignored: bool, expected: int) -> None:
    assert target_directory_mode(ignored=ignored) == expected


def test_documented_mode_values_are_the_cross_uid_policy() -> None:
    assert SHARED_FILE_MODE == 0o660
    assert SHARED_EXECUTABLE_MODE == 0o770
    assert SHARED_DIRECTORY_MODE == 0o2770
    assert PRIVATE_FILE_MODE == 0o600
    assert PRIVATE_EXECUTABLE_MODE == 0o700
    assert PRIVATE_DIRECTORY_MODE == 0o700


def test_git_classifier_trusts_only_the_exact_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    seen: list[list[str]] = []

    def recording_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        "factory.integrations.workspace.shared_policy.subprocess.run",
        recording_run,
    )
    classify_ignored_paths(root)
    assert len(seen) == 2
    for argv in seen:
        assert argv[:4] == ["git", "-c", f"safe.directory={root}", "ls-files"]
        assert "safe.directory=*" not in argv


def test_two_phase_api_classifies_then_normalizes(tmp_path: Path) -> None:
    # The provisioner and the hook use the same two phases; this proves the
    # classification and the normalization agree without re-reading git.
    root = _repo(tmp_path)
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    secret = root / ".env"
    secret.write_text("x\n", encoding="utf-8")
    secret.chmod(0o644)
    shared = root / "shared.txt"
    shared.write_text("y\n", encoding="utf-8")
    shared.chmod(0o600)

    ignored_files, ignored_directories = classify_ignored_paths(root)
    assert ".env" in ignored_files
    assert ignored_directories == ()
    normalize_workspace(root, ignored_files=ignored_files, ignored_directories=ignored_directories)

    assert _mode(secret) == PRIVATE_FILE_MODE
    assert _mode(shared) == SHARED_FILE_MODE


# -- normal nodes ----------------------------------------------------------


def test_normalizes_shared_regular_executable_and_directory(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    regular = root / "notes.txt"
    regular.write_text("agent result\n", encoding="utf-8")
    regular.chmod(0o600)
    script = root / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o700)
    package = root / "pkg"
    package.mkdir()

    normalize_workspace_path(root)

    assert _mode(regular) == SHARED_FILE_MODE
    assert _mode(script) == SHARED_EXECUTABLE_MODE
    assert _mode(package) == SHARED_DIRECTORY_MODE
    assert _mode(root) == SHARED_DIRECTORY_MODE
    # Contents are never rewritten by a permission repair.
    assert regular.read_text(encoding="utf-8") == "agent result\n"
    assert script.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


def test_new_0600_openhands_owned_file_becomes_group_readable(tmp_path: Path) -> None:
    # A file_editor/create leaves a brand-new file at 0600 regardless of umask,
    # because NamedTemporaryFile is created owner-only and the destination did
    # not exist to copy a mode from. Owner-side normalization must fix it.
    root = _repo(tmp_path)
    created = root / "new_module.py"
    created.write_text("VALUE = 1\n", encoding="utf-8")
    created.chmod(0o600)
    assert _mode(created) == 0o600

    normalize_workspace_path(root)

    assert _mode(created) == SHARED_FILE_MODE
    assert created.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_umask_does_not_affect_the_normalized_result(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    target = root / "umask_test.txt"
    previous = os.umask(0o022)
    try:
        target.write_text("x\n", encoding="utf-8")
        target.chmod(0o644)
    finally:
        os.umask(previous)

    normalize_workspace_path(root)

    assert _mode(target) == SHARED_FILE_MODE


# -- ignored / private nodes ----------------------------------------------


def test_compliant_ignored_nodes_are_left_private_and_opaque(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".gitignore").write_text(".env\nprivate/\n", encoding="utf-8")
    secret = root / ".env"
    secret.write_text("TEST_ONLY=1\n", encoding="utf-8")
    secret.chmod(0o600)
    private = root / "private"
    private.mkdir()
    nested = private / "nested"
    nested.mkdir()
    nested_secret = nested / "secret.txt"
    nested_secret.write_text("hidden\n", encoding="utf-8")
    private.chmod(0o700)
    nested_before = _mode(nested)
    nested_secret_before = _mode(nested_secret)

    normalize_workspace_path(root)

    assert _mode(secret) == PRIVATE_FILE_MODE
    assert _mode(private) == PRIVATE_DIRECTORY_MODE
    # The ignored subtree is opaque: nothing inside it was traversed, so its
    # children keep exactly the modes they had (umask-dependent, hence recorded).
    assert _mode(nested) == nested_before
    assert _mode(nested_secret) == nested_secret_before


def test_ignored_only_gitignore_is_itself_not_traversed_as_private(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    build = root / "build"
    build.mkdir()
    artifact = build / "out.bin"
    artifact.write_bytes(b"x")
    build.chmod(0o700)

    normalize_workspace_path(root)

    assert _mode(build) == PRIVATE_DIRECTORY_MODE


def test_ignored_contents_cannot_enter_the_validated_git_revision(tmp_path: Path) -> None:
    from factory.domain.models import Workspace
    from factory.integrations.workspace.revision import GitWorkspaceRevisionInspector

    root = _repo(tmp_path)
    (root / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (root / "src.py").write_text("A\n", encoding="utf-8")
    cache = root / "cache"
    cache.mkdir()
    (cache / "state").write_text("one\n", encoding="utf-8")
    cache.chmod(0o700)
    workspace = Workspace(repository_slug="example/target", branch="factory/x/ws", path=str(root))
    inspector = GitWorkspaceRevisionInspector()
    before = inspector.fingerprint(workspace)

    # Mutating the private, ignored cache must not change the publishable tree.
    (cache / "state").write_text("two\n", encoding="utf-8")
    normalize_workspace_path(root)

    assert inspector.fingerprint(workspace) == before


# -- link and special-file refusal ----------------------------------------


def test_symlink_is_never_followed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("unchanged\n", encoding="utf-8")
    outside.chmod(0o600)
    (root / "link").symlink_to(outside)
    (root / "dir-link").symlink_to(root / "pkg", target_is_directory=True)

    normalize_workspace_path(root)

    assert _mode(outside) == 0o600
    assert outside.read_text(encoding="utf-8") == "unchanged\n"
    assert (root / "link").is_symlink()


def test_special_file_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    os.mkfifo(root / "pipe")

    with pytest.raises(SharedWorkspacePolicyError):
        normalize_workspace_path(root)


def test_hardlink_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    os.link(root / "README.md", root / "hardlink")

    with pytest.raises(SharedWorkspacePolicyError):
        normalize_workspace_path(root)


def test_missing_git_metadata_fails_closed(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "file.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(SharedWorkspacePolicyError):
        normalize_workspace_path(plain)


def test_unknown_node_classification_is_refused(tmp_path: Path) -> None:
    # A non-directory root cannot be classified; the policy fails closed rather
    # than silently returning "nothing ignored".
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x\n", encoding="utf-8")
    with pytest.raises(SharedWorkspacePolicyError):
        normalize_workspace_path(not_a_dir)


# -- foreign ownership -----------------------------------------------------


def test_compliant_nodes_skip_chmod_so_foreign_ownership_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    (root / ".gitignore").write_text(".env\nprivate/\n", encoding="utf-8")
    root.chmod(SHARED_DIRECTORY_MODE)
    (root / "README.md").chmod(SHARED_FILE_MODE)
    script = root / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(SHARED_EXECUTABLE_MODE)
    secret = root / ".env"
    secret.write_text("x\n", encoding="utf-8")
    secret.chmod(PRIVATE_FILE_MODE)
    private = root / "private"
    private.mkdir()
    private.chmod(PRIVATE_DIRECTORY_MODE)
    # .gitite file is tracked? It is untracked and not ignored, so 0660.
    (root / ".gitignore").chmod(SHARED_FILE_MODE)

    nodes = [
        root,
        script,
        secret,
        private,
        root / "run.sh",
        root / ".gitignore",
        root / "README.md",
    ]
    attempted = _deny_chmod(monkeypatch, nodes)

    normalize_workspace_path(root)

    assert attempted == []


def test_unrepairable_noncompliant_node_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    node = root / "foreign.txt"
    node.write_text("x\n", encoding="utf-8")
    node.chmod(0o640)
    before = node.stat().st_mode
    attempted = _deny_chmod(monkeypatch, [node])

    with pytest.raises(SharedWorkspacePolicyError) as caught:
        normalize_workspace_path(root)
    assert len(attempted) == 1
    assert node.stat().st_mode == before
    assert caught.value.__context__ is None
    assert str(tmp_path) not in str(caught.value)
    assert "foreign" not in str(caught.value)


# -- OpenHands hook CLI ----------------------------------------------------


def _run_hook(monkeypatch: pytest.MonkeyPatch, event: str) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(event))
    return main()


def test_hook_repairs_a_compliant_workspace_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    created = root / "created.md"
    created.write_text("result\n", encoding="utf-8")
    created.chmod(0o600)

    code = _run_hook(monkeypatch, f'{{"working_dir": "{root}"}}')

    assert code == 0
    assert _mode(created) == SHARED_FILE_MODE


def test_hook_blocks_when_policy_cannot_be_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "no-git"
    plain.mkdir()
    (plain / "loose.txt").write_text("x\n", encoding="utf-8")
    (plain / "loose.txt").chmod(0o644)

    code = _run_hook(monkeypatch, f'{{"working_dir": "{plain}"}}')

    assert code == 2


@pytest.mark.parametrize(
    "event",
    ["", "not json", "{}", '{"working_dir": "relative/path"}', '{"working_dir": 7}'],
)
def test_hook_fails_closed_on_an_unusable_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    _repo(tmp_path)
    assert _run_hook(monkeypatch, event) == 2


def test_hook_fails_closed_on_a_symlinked_working_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)

    assert _run_hook(monkeypatch, f'{{"working_dir": "{link}"}}') == 2


def test_hook_command_works_as_a_real_subprocess(tmp_path: Path) -> None:
    # Exercise the executable contract the OpenHands executor uses: a command
    # that receives the event JSON on stdin and signals a block through exit 2.
    src = Path(__file__).resolve().parent.parent / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    root = _repo(tmp_path)
    created = root / "created.py"
    created.write_text("VALUE = 1\n", encoding="utf-8")
    created.chmod(0o600)

    completed = subprocess.run(
        [sys.executable, "-m", "factory.integrations.workspace.shared_policy"],
        input=f'{{"working_dir": "{root}"}}',
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0
    assert _mode(created) == SHARED_FILE_MODE


def test_hook_subprocess_blocks_when_policy_cannot_be_enforced(tmp_path: Path) -> None:
    src = Path(__file__).resolve().parent.parent / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    plain = tmp_path / "no-git"
    plain.mkdir()
    (plain / "loose.txt").write_text("x\n", encoding="utf-8")
    (plain / "loose.txt").chmod(0o600)

    completed = subprocess.run(
        [sys.executable, "-m", "factory.integrations.workspace.shared_policy"],
        input=f'{{"working_dir": "{plain}"}}',
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 2
    assert str(plain) not in completed.stderr
