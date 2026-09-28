"""Single source of truth for the cross-UID shared-workspace permission policy.

The Factory runs as one host UID and OpenHands as another, with a shared
supplementary group. A workspace both processes must read therefore has to be
group-accessible, while ignored/private files (credentials, caches) must stay
owner-only. This module is the *only* place that decides what "shared" means;
the Factory-side provisioner and the OpenHands-side owner normalizer both call
it, so the two can never drift into incompatible policies.

```
shared regular file   0660   owner+group rw
shared executable     0770   owner+group rwx
shared directory      2770   setgid, owner+group rwx
private file          0600   owner rw
private executable    0700   owner rwx
private directory     0700   owner rwx
```

Why a process umask is not enough: OpenHands' ``file_editor/create`` writes a
new file through ``NamedTemporaryFile`` (explicitly ``0600``) and only copies
the destination's mode when it already exists, so a brand-new file keeps
``0600`` regardless of ``umask``. Owner-side normalization is the only reliable
repair; ``umask 0007`` merely helps ordinary creation.

Safety properties, all enforced by :func:`normalize_workspace`:

* argv-only, shell-free git classification, with a bounded timeout;
* symlinks are never followed and their targets are never touched;
* hardlinked and special (non-regular, non-directory) nodes fail closed;
* already-compliant nodes skip ``chmod``, so a foreign-owned compliant node
  needs no ownership privileges;
* ignored/private directories are opaque: they are never opened to read and
  their children are never inspected or normalized;
* any failure raises :class:`SharedWorkspacePolicyError`, a sanitized error
  whose message carries no path, inode or raw git output.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

#: Mode of a shared, non-executable regular file.
SHARED_FILE_MODE = 0o660
#: Mode of a shared executable regular file.
SHARED_EXECUTABLE_MODE = 0o770
#: Mode of a shared directory (setgid, so children inherit the group).
SHARED_DIRECTORY_MODE = 0o2770
#: Mode of a private, non-executable regular file.
PRIVATE_FILE_MODE = 0o600
#: Mode of a private executable regular file.
PRIVATE_EXECUTABLE_MODE = 0o700
#: Mode of a private directory.
PRIVATE_DIRECTORY_MODE = 0o700

#: Default per-command timeout. Classification is local and cheap.
DEFAULT_TIMEOUT_SECONDS = 60.0

#: Environment variables git legitimately needs. Everything else — including any
#: credential the calling process happens to hold — is not forwarded.
_ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT")


class SharedWorkspacePolicyError(RuntimeError):
    """A workspace cannot be brought to the shared permission policy.

    Raised for both an unrepairable/unsafe node and an unavailable classifier
    (for example git metadata that is not mounted). The message is a fixed
    constant so no path, inode, symlink target or raw git output can leak.
    """

    def __init__(self) -> None:
        super().__init__("shared workspace could not be normalized to permission policy")


def target_file_mode(*, ignored: bool, executable: bool) -> int:
    """Return the required mode of a regular file under the shared policy."""
    if ignored:
        return PRIVATE_EXECUTABLE_MODE if executable else PRIVATE_FILE_MODE
    return SHARED_EXECUTABLE_MODE if executable else SHARED_FILE_MODE


def target_directory_mode(*, ignored: bool) -> int:
    """Return the required mode of a directory under the shared policy."""
    return PRIVATE_DIRECTORY_MODE if ignored else SHARED_DIRECTORY_MODE


def classify_ignored_paths(
    root: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Classify the paths Git ignores under ``root`` using git's own rules.

    Returns ``(ignored_files, ignored_directories)`` where directory names carry
    a trailing ``/``. Raises :class:`SharedWorkspacePolicyError` when git cannot
    answer, rather than returning an empty classification: an unavailable
    classifier must fail closed.

    Read-only git metadata is required. A Git worktree records it outside the
    workspace (``.git`` is a pointer file), so the deployment must expose that
    metadata read-only; see ``docs/openhands-shared-workspace-runtime.md``.
    """
    files_output = _git_ls_files(
        root, ("--others", "--ignored", "--exclude-standard", "-z"), timeout=timeout
    )
    directory_output = _git_ls_files(
        root,
        ("--others", "--ignored", "--exclude-standard", "--directory", "-z"),
        timeout=timeout,
    )
    ignored_files = frozenset(name for name in files_output.split("\0") if name)
    ignored_directories = tuple(name for name in directory_output.split("\0") if name.endswith("/"))
    return ignored_files, ignored_directories


def normalize_workspace(
    root: Path,
    *,
    ignored_files: frozenset[str],
    ignored_directories: tuple[str, ...],
) -> None:
    """Bring every shared node under ``root`` to the permission policy.

    Must be called with git's ignored-path classification so ignored/private
    credentials and caches stay owner-only. Raises
    :class:`SharedWorkspacePolicyError` on the first unsafe or unrepairable node.
    """
    try:
        _normalize_tree(
            root,
            ignored_files=ignored_files,
            ignored_directories=ignored_directories,
        )
    except OSError:
        # Includes os.fwalk's onerror path; the raw OS error can name a path, so
        # it is discarded. The policy error is raised outside the ``except`` block
        # so the discarded error is not retained as ``__context__``.
        pass
    else:
        return
    raise SharedWorkspacePolicyError()


def normalize_workspace_path(root: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
    """Classify ignored paths under ``root`` and normalize the whole workspace."""
    ignored_files, ignored_directories = classify_ignored_paths(root, timeout=timeout)
    normalize_workspace(root, ignored_files=ignored_files, ignored_directories=ignored_directories)


def _normalize_tree(
    root: Path,
    *,
    ignored_files: frozenset[str],
    ignored_directories: tuple[str, ...],
) -> None:
    def fail_walk(error: OSError) -> None:
        raise error

    for directory, dirnames, files, directory_fd in os.fwalk(
        root, follow_symlinks=False, onerror=fail_walk
    ):
        relative_directory = Path(directory).relative_to(root).as_posix() + "/"
        directory_mode = target_directory_mode(
            ignored=relative_directory.startswith(ignored_directories)
        )
        if stat.S_IMODE(os.fstat(directory_fd).st_mode) != directory_mode:
            os.fchmod(directory_fd, directory_mode)
        for name in list(dirnames):
            relative = (Path(directory) / name).relative_to(root).as_posix() + "/"
            if relative not in ignored_directories:
                continue
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise SharedWorkspacePolicyError()
            if stat.S_IMODE(info.st_mode) != PRIVATE_DIRECTORY_MODE:
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or stat.S_ISLNK(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    ):
                        raise SharedWorkspacePolicyError()
                    os.fchmod(child_fd, PRIVATE_DIRECTORY_MODE)
                finally:
                    os.close(child_fd)
            # A compliant ignored directory is intentionally opaque: fwalk must
            # never try to open it or inspect its children.
            dirnames.remove(name)
        for name in files:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                continue
            # Stat first: a compliant foreign-owned ignored secret may be
            # owner-readable only, so even opening it is unnecessary.
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SharedWorkspacePolicyError()
            relative = (Path(directory) / name).relative_to(root).as_posix()
            executable = bool(info.st_mode & stat.S_IXUSR)
            mode = target_file_mode(ignored=relative in ignored_files, executable=executable)
            if stat.S_IMODE(info.st_mode) == mode:
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            try:
                opened = os.fstat(fd)
                # Refuse replacements and hardlinks before chmod can affect
                # anything outside the isolated workspace.
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise SharedWorkspacePolicyError()
                if stat.S_IMODE(opened.st_mode) != mode:
                    os.fchmod(fd, mode)
            finally:
                os.close(fd)


def _git_ls_files(root: Path, args: tuple[str, ...], *, timeout: float) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - argv form, shell is never used
            ["git", "ls-files", *args],
            cwd=str(root),
            env=_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise SharedWorkspacePolicyError() from None
    if completed.returncode != 0:
        raise SharedWorkspacePolicyError()
    return os.fsdecode(completed.stdout)


def _env() -> dict[str, str]:
    """Minimal, secret-free environment for git subprocesses."""
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    # Never prompt for credentials, and never let a helper cache one.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the OpenHands hook (``PostToolUse`` and ``Stop``).

    Reads the hook event JSON on stdin, normalizes ``working_dir``, and returns
    process exit codes matching the OpenHands hook contract:

    * ``0`` — the workspace is compliant (or was repaired);
    * ``2`` — **blocking** failure: the workspace is unsafe or unrepairable, so a
      ``Stop`` hook prevents the agent from reporting success.

    ``working_dir`` is the conversation's working directory, which the factory
    sets to the run's isolated workspace. A missing, relative or symlinked
    ``working_dir`` fails closed. Nothing but a fixed, path-free message is
    emitted, so no workspace path or git output can leak into the event log.
    """
    del argv  # The hook contract passes the event on stdin, not as arguments.
    root = _working_dir_from_stdin()
    if root is None:
        return _blocked()
    try:
        normalize_workspace_path(root)
    except SharedWorkspacePolicyError:
        return _blocked()
    return 0


def _working_dir_from_stdin() -> Path | None:
    try:
        raw = sys.stdin.read()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    working_dir = event.get("working_dir")
    if not isinstance(working_dir, str) or not working_dir:
        return None
    root = Path(working_dir)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        return None
    return root


def _blocked() -> int:
    sys.stderr.write("ai-factory-lab: shared workspace permission policy not satisfied\n")
    return 2


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_EXECUTABLE_MODE",
    "PRIVATE_FILE_MODE",
    "SHARED_DIRECTORY_MODE",
    "SHARED_EXECUTABLE_MODE",
    "SHARED_FILE_MODE",
    "SharedWorkspacePolicyError",
    "classify_ignored_paths",
    "normalize_workspace",
    "normalize_workspace_path",
    "target_directory_mode",
    "target_file_mode",
]


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess in tests
    raise SystemExit(main())
