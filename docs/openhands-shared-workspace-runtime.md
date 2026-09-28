# Shared-workspace runtime: owner-side normalization and the hook

This is the runbook for the runtime support that makes a shared workspace safe
for repeated cross-UID use. It implements the proposal in the read-only audit
[`openhands-shared-workspace-audit.md`](openhands-shared-workspace-audit.md).

The problem, in one line: the Factory runs as host UID 1000 and OpenHands as UID
10001 (supplementary GID 1000), and OpenHands' `file_editor/create` atomically
creates a new file as `0600` through `NamedTemporaryFile`, regardless of umask,
so the Factory cannot fingerprint or validate the workspace.

## One policy, two callers

The permission policy lives in exactly one module:
`factory/integrations/workspace/shared_policy.py`. Both callers use it, so the
policies cannot drift:

| Node | Mode |
|---|---|
| shared regular file | `0660` |
| shared executable file | `0770` |
| shared directory | `2770` (setgid) |
| private / ignored file | `0600` |
| private / ignored executable | `0700` |
| private / ignored directory | `0700` |

Callers:

1. **Factory-side repair (defense in depth).** `GitWorktreeWorkspaceProvisioner`
   (`integrations/workspace/git.py`) normalizes during `prepare`/`repair`, before
   fingerprints and gates. This is the post-agent repair merged in PR #12 and is
   kept unchanged in behaviour; it now delegates its modes and ignored-path
   classification to the shared policy.
2. **OpenHands owner-side normalization (the fix).** The same module is the
   executable the OpenHands conversation runs as a hook, as UID 10001, so a
   `0600` file it just created is made group-readable by its owner, without host
   root. This is what makes the workspace safe *before* the Factory sees it.

The policy denies or refuses: symlinks are never followed, hardlinked and special
(non-regular, non-directory) nodes fail closed, and a compliant ignored directory
is opaque — its children are never opened, read, or normalized. Ignored/private
contents therefore cannot enter the validated Git revision (the revision is a
Git tree object id, which excludes ignored paths).

## How the hook is wired

The audit established that merely placing a user `hooks.json` does **not**
activate hooks in the supported server (1.49.6); the hook configuration must
travel with *conversation creation* as `hook_config`. The adapter now does that:

- `OpenHandsExecution.shared_workspace_hook_command` (env
  `OPENHANDS_SHARED_WORKSPACE_HOOK_COMMAND`) is rendered into a `hook_config`
  payload by `OpenHandsExecution.hook_payload()` and attached by
  `build_creation_payload`;
- `PostToolUse` matched on `file_editor` runs the command **synchronously** after
  each editor write, so a newly created `0600` file is normalized immediately;
- `Stop` runs the same command, and because the hook exits `2` when it cannot
  enforce the policy, OpenHands **blocks the agent from reporting success** on an
  unsafe or unrepairable workspace.

The command receives the hook event as JSON on stdin; the workspace is the
event's `working_dir` (the conversation's working directory, which the factory
sets to the run's isolated workspace). A missing, relative or symlinked
`working_dir`, a non-Git workspace, an unreadable Git classification, or any
unsafe node all fail closed with exit `2` and a fixed, path-free stderr message.

Example `hook_config` produced for a conversation:

```json
{
  "PostToolUse": [
    {"matcher": "file_editor", "hooks": [
      {"type": "command", "command": "<OPENHANDS_SHARED_WORKSPACE_HOOK_COMMAND>",
       "timeout": 60, "async": false}]}
  ],
  "Stop": [
    {"hooks": [
      {"type": "command", "command": "<OPENHANDS_SHARED_WORKSPACE_HOOK_COMMAND>",
       "timeout": 60, "async": false}]}
  ]
}
```

## Installing / recreating the reviewed runtime artifacts

This section describes how an operator installs the artifacts that the Factory
*emits* — the code in this repository is the source of truth, but the container
runtime is configured out of band. **A change to any of these requires container
recreation, not a restart.** Nothing here is applied by the Factory itself.

1. **Install the normalizer into the OpenHands container from the exact reviewed
   revision.** The hook command must be runnable inside the container as UID
   10001. AI Factory Lab is not assumed to be published on PyPI, so do **not**
   rely on `pip install ai-factory-lab` by package name.

   Preferred deployment shape:

   - check out the exact Git commit that was reviewed and merged;
   - build a wheel from that revision (for example, `python -m build --wheel`);
   - copy that immutable wheel into the OpenHands image/build context; and
   - install that wheel in the recreated image/container so `factory-hook` (or
     `python -m factory.integrations.workspace.shared_policy`) is on `PATH`.

   A standalone reviewed script at a fixed path is acceptable only when it is
   generated from the same reviewed revision and imports the same policy module;
   do not maintain a hand-edited second implementation.

   Record the installed Git SHA/wheel artifact in the operator change record so
   the running helper can be traced back to the reviewed source.

2. **Expose the run's Git metadata read-only.** The ignored-path classification
   uses `git ls-files`. Because the Factory-created worktree is owned by host UID
   1000 while the hook runs as OpenHands UID 10001, each classification command
   trusts only its already-validated absolute workspace path via
   `git -c safe.directory=<working-dir> ...`; never configure a global
   `safe.directory=*`. A worktree records its Git metadata outside the
   workspace (`.git` is a pointer file). The container mounts currently expose
   only `/projects` and state, so the metadata directory (for example
   `/srv/ai-factory/control-plane/.git/worktrees/...` and the shared object
   store) must be mounted **read-only** at the same logical location the
   working directory resolves to. Without it, the hook fails closed (exit `2`)
   and the agent cannot report success — which is the safe outcome, but it blocks
   legitimate runs.

3. **Preserve the cross-UID group and setgid roots.** Keep the existing user
   (`openhands`, UID/GID 10001), supplementary GID 1000, and the setgid `2770`
   workspace roots. Do not change ownership.

4. **Optionally set `umask 0007` for ordinary creation.** Preserve the image
   entrypoint and set the umask before it:

   ```yaml
   services:
     openhands:
       entrypoint:
         - tini
         - --
         - /bin/sh
         - -c
         - 'umask 0007; exec /opt/agent-canvas/entrypoint.sh "$@"'
         - --
   ```

   This makes ordinary `0666`/`0777` creation land at `0660`/`2770`. It is a
   convenience only: it **must not** be treated as sufficient, because it cannot
   add group access to a temporary explicitly created `0600`.

5. **Point the Factory at the hook command** (host side, in `.env`):

   ```bash
   OPENHANDS_SHARED_WORKSPACE_HOOK_COMMAND=python -m factory.integrations.workspace.shared_policy
   ```

   This value is passed as `hook_config` on every new conversation. It is
   configuration, not a secret, and appears unmasked in `--show-config`.

6. **Recreate the container** so the entrypoint and mounts above take effect. The
   old container's configuration is not enough; a plain restart reuses it. New
   hook configuration also requires a new (or explicitly reconfigured)
   conversation, because it is carried in the creation payload.

## Self-development validation profile (cache-neutral gates)

OpenHands correctly leaves ignored/private caches (`.pytest_cache/`,
`.ruff_cache/`, `.mypy_cache/`) owner-only. Host-side Factory gates run as the
Factory user and must **not** need to write into those directories. For the
self-development validation profile, use cache-neutral invocations:

```bash
FACTORY_QUALITY_GATES=[\
{"name":"tests","argv":["pytest","-p","no:cacheprovider"],"required":true},\
{"name":"lint","argv":["ruff","check","--no-cache","."],"required":true},\
{"name":"format","argv":["ruff","format","--check","--no-cache","."],"required":true},\
{"name":"types","argv":["mypy","--cache-dir=/dev/null"],"required":true}]
```

These keep private cache contents opaque (the policy never traverses them) and
avoid a gate that would require group write to an owner-only cache.

## Bootstrap safeguard for this self-hosted run

This Issue was implemented by the Factory *before* the permanent owner-side hook
existed. As a one-time safeguard, the implementing run normalized every
repository path it created or replaced, owner-side, before reporting completion
(`0660` shared files, `0770` shared executables, `2770` shared directories,
`0600` ignored/private regular files, and `0700` ignored/private executables and
directories), without world-readable fallbacks and without changing ownership. With the permanent hook above installed, future runs no longer need
task-specific instructions.

## Verification

- `pytest tests/test_shared_workspace_policy.py` — policy modes, owner-side
  normalization of a new `0600` file, opaque ignored nodes, symlink/special/
  hardlink refusal, and the hook CLI exit codes.
- `pytest tests/test_shared_workspace_stop_hook.py` — the hook run as a real
  subprocess against a disposable worktree; the Stop hook blocks (exit `2`) and
  the Factory still fails closed with no bound revision.
- `pytest tests/test_shared_workspace_permissions.py` — the existing Factory-side
  repair behaviour is unchanged.
- `pytest tests/test_openhands_execution.py tests/test_openhands_dispatch_integration.py`
  — the `hook_config` is attached to the conversation-creation payload.
