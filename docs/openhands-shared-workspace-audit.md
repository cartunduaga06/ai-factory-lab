# Cross-UID workspace audit (read-only)

Observed on 2026-09-28, with no runtime changes or agent execution:

- Compose: `/srv/ai-factory/openhands/compose.yml`, service `openhands`, image
  `ghcr.io/openhands/agent-canvas:1.24.0`, `init: true`, supplementary group `1000`.
  It has no entrypoint, command or user override. Only workspace and state binds
  are configured: `/srv/ai-factory/workspaces:/projects` and
  `/srv/ai-factory/openhands/state:/home/openhands/.openhands`.
- Image entrypoint: `["tini", "--", "/opt/agent-canvas/entrypoint.sh"]`; command
  is null; user is `openhands` (UID/GID 10001, supplementary GID 1000).
- PID 1 (`docker-init`), OpenHands server PID 11 and its child PID 27 all have
  `Umask: 0022`. A shell executed as `openhands` reports `0022`; the existing
  terminal shell PID 11355 also has `0022`.
- No umask setter was found in the installed OpenHands Python packages or the
  Agent Canvas entrypoint. Installed server, SDK and tools are version 1.49.6.
- The current run's event metadata records `file_editor`, command `create`, for
  `ai-factory-acceptance-phase5.md`. No file contents or credentials were printed.
- Installed `openhands/tools/file_editor/editor.py`, lines 482–528, uses
  `NamedTemporaryFile` followed by replacement. It copies mode bits only when
  the destination already exists. New files retain the temporary's `0600`.
  `sdk/utils/files.py` additionally has an explicitly owner-only atomic writer;
  that helper must remain private for credential/state use.

Thus `0007` alone is **not sufficient**: it masks requested permission bits; it
cannot add group access to a temporary explicitly created as `0600`. The observed
0600 is explained by the editor's new-file atomic write, not the observed 0022.
Existing 0600 files also remain unchanged after a umask change.

## Minimal persistent runtime proposal (not applied)

For ordinary creation, preserve the image entrypoint and set umask before it:

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

Keep the existing user, supplementary group and setgid workspace roots. Ordinary
`0666` file creation then yields `0660`; `0777` directory creation under a setgid
`2770` parent yields `2770`. Explicit restrictive creation still needs owner-side
normalization. No global change to Python tempfile or secret writers is suitable.

The smallest supported extension is a synchronous command hook executed by
OpenHands as UID 10001: `PostToolUse` for `file_editor`, plus a `Stop` hook that
performs/checks the same access policy and exits **2** if it cannot enforce it.
PostToolUse alone is not blocking. Supply the hook configuration when creating
conversations; merely placing a user hooks.json does not activate hooks in this
installed version. Its hooks service labels user hooks as future support.

Illustrative hook configuration, requiring a reviewed normalizer installed at
this path before activation (this file is not installed by PR #12):

```json
{
  "PostToolUse": [{
    "matcher": "file_editor",
    "hooks": [{"type": "command", "command": "python /opt/ai-factory/repair_shared_workspace.py", "timeout": 60, "async": false}]
  }],
  "Stop": [{
    "hooks": [{"type": "command", "command": "python /opt/ai-factory/repair_shared_workspace.py || exit 2", "timeout": 60, "async": false}]
  }]
}
```

That normalizer must reuse the provisioner's policy, accept only the isolated
workspace, preserve executable bits and ignored secrets, refuse links/special
files, and skip already-compliant foreign inodes. It must have read-only access
to the appropriate Git metadata to classify ignored paths and confirm identity:
the current container mounts only `/projects` and state, not the source Git
metadata. Packaging the policy/helper, exposing Git metadata read-only, and
wiring hooks into conversation creation require a separate reviewed runtime
change. No hook helper or new mount has been applied or tested in production.

The compose wrapper/mount changes require container recreation, not merely
restarting its old configuration. New hook configuration requires a new or
explicitly reconfigured conversation. No OpenHands rerun is needed to repair the
existing acceptance file later: its owner can normalize permissions without
changing content, followed by the guarded Factory revalidation.

## Status update: versioned runtime support (code only)

The reviewed owner-side normalizer and its hook wiring are now implemented and
versioned in this repository; they are **not** installed on the host by this
change:

- the shared permission policy is the single module
  `src/factory/integrations/workspace/shared_policy.py`, used both by the
  Factory-side provisioner repair and by the hook executable;
- the OpenHands adapter attaches the supported conversation `hook_config`
  (`PostToolUse` on `file_editor`, plus `Stop`) from
  `OPENHANDS_SHARED_WORKSPACE_HOOK_COMMAND`;
- the executable reads the hook event on stdin, normalizes `working_dir`
  owner-side, and exits `2` when it cannot enforce the policy (so `Stop` blocks
  a successful completion).

Installing the helper into the container, exposing the run's Git metadata
read-only, and recreating the container remain human, out-of-band steps. The
installation/runbook is
[`openhands-shared-workspace-runtime.md`](openhands-shared-workspace-runtime.md);
the compose wrapper and its `umask 0007` remain a convenience, never the fix.

Sources: installed code and `/proc` observations above;
[OpenHands hook contract](https://docs.openhands.dev/sdk/guides/hooks) documents
PostToolUse/Stop semantics and exit code 2. Runtime readiness remains blocked on
installing and verifying owner-side normalization in the container; the umask
wrapper alone is not an operational fix.
