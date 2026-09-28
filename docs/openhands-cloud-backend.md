# OpenHands Cloud backend — operator runbook

This runbook covers running the factory with the **OpenHands Cloud** execution
backend. Cloud is optional and never the default; local mode is unaffected by
anything here.

## What Cloud changes, and what it does not

A Cloud run executes in an OpenHands sandbox. The factory provisions an isolated
local worktree at the configured base ref, records its immutable HEAD, creates a
Git bundle locally, and uploads that binary through the supported Agent Server
file API. Factory-controlled bash imports the bundle into a safe sandbox path and
checks the exact branch and base SHA before conversation creation. The conversation
has no GitHub credential and the agent does no network Git operation.

On terminal success, the factory verifies the sandbox branch, base ancestry, and
clean worktree, reads HEAD through the supported Git API, creates a result bundle
in the sandbox, and downloads it through the binary file API. It verifies the
bundle branch and SHA under a temporary local ref, then applies its exact tree
to the run's local worktree while retaining the base HEAD. Local gates bind
`validated_revision`; only the existing local
publisher may push and open a PR. The task stops at `WAITING_HUMAN`.

Any uncertain identity, bundle, branch, ancestry, or transfer fails closed. After
terminal state and task reconciliation are durable, sandbox release is best-effort
and idempotent. A paused sandbox is resumed before collection.

**Follow-up before merge:** sandbox creation still precedes durable run persistence.
A process crash in that small window can orphan a sandbox; closing it requires a
larger transactional lifecycle design.

## Configuration

Set `OPENHANDS_BACKEND=cloud` plus the Cloud settings. All values are optional
until Cloud is selected, and none is logged or committed (see `.env.example`):

| Variable | Purpose |
|---|---|
| `OPENHANDS_BACKEND` | `local` (default) or `cloud`. |
| `OPENHANDS_CLOUD_API_KEY` | Cloud API bearer credential. Required for Cloud. |
| `OPENHANDS_CLOUD_REPOSITORY` | Exact `owner/name` repository Cloud works in. Required and must equal `FACTORY_TARGET_REPO`. |
| `OPENHANDS_CLOUD_API_URL` | Cloud API base URL. Default `https://app.all-hands.dev`. |
| `OPENHANDS_CLOUD_WORKING_DIR` | Exact sandbox directory used for the repository checkout. |
| `OPENHANDS_CLOUD_BASE_REF` | Optional commit-ish the isolated branch starts from; otherwise workspace/default branch config is used. |
| `OPENHANDS_CLOUD_PROFILE` | Exact Agent Profile UUID resolvable by the new sandbox's Agent Server. Required. |
| `OPENHANDS_CLOUD_MODEL` | Direct model selector is unsupported on this Agent Server path; if set, startup fails closed. |

Credentials come from the environment or a secret store. The factory never
handles an LLM key when an agent profile is used: the sandbox resolves it.

### Profile selection is explicit; there is no silent paid-model fallback

The Cloud backend requires an exact **Agent Profile UUID** that the new sandbox's
Agent Server can resolve. Configure the desired model in that server-side Agent
Profile, then set `OPENHANDS_CLOUD_PROFILE` to its UUID. A SaaS LLM profile name
is not an Agent Profile UUID; this adapter does not retrieve or translate SaaS
LLM profile settings. The Factory does not infer a profile,
does not silently choose a default model, and does not automatically fall back
to another model.

`OPENHANDS_CLOUD_MODEL` is retained only as a compatibility guard: if it is
set, startup fails closed because direct model selection is not part of the
Agent Server conversation contract used by this adapter. This prevents the
operator from believing a specific/free model was enforced when it was not.

### Failing closed

- `OPENHANDS_BACKEND=cloud` with a missing API key, repository or Agent Profile
  UUID → the factory refuses to start with a `BackendConfigurationError`. It does
  **not** run local instead.
- `OPENHANDS_CLOUD_REPOSITORY` differing from `FACTORY_TARGET_REPO` → startup
  fails before a task is claimed.
- A non-UUID profile or a direct `OPENHANDS_CLOUD_MODEL` selector → startup
  fails closed rather than silently selecting something else.
- A Cloud dispatch/collect failure → a sanitized error; the run follows the
  existing failure path. There is no local↔cloud fallback.
- A terminal success whose revision cannot be retrieved or imported → the run
  is `FAILED`; nothing is published.

### Redaction check

```
python -m factory --show-config
```

The Cloud API key must appear as `"***"` (or `null` when unset), and the bearer
key must never appear in any log line, error message or URL.


Private target repositories need no GitHub credential in the Cloud sandbox.
The local provisioner must already have the selected base revision. The local
publisher alone uses its existing write credential after successful local gates.

## Manual smoke test (no paid-model fallback)

This procedure verifies the wiring without purchasing credit and without
switching the production/default factory to Cloud.

1. **Confirm local mode still works.** Leave `OPENHANDS_BACKEND` unset and run
   the four repository checks; local dispatch and the Phase 5 tests must be
   unaffected.

   ```bash
   pytest && ruff check . && ruff format --check . && mypy
   ```

2. **Confirm Cloud is opt-in.** With Cloud credentials absent, select Cloud and
   observe a clean refusal before any task is claimed:

   ```bash
   OPENHANDS_BACKEND=cloud python -m factory run
   # -> BackendConfigurationError: the cloud backend requires
   #    OPENHANDS_CLOUD_API_KEY and OPENHANDS_CLOUD_REPOSITORY
   ```

3. **Confirm redaction.** Export a throwaway Cloud key and repository, then:

   ```bash
   python -m factory --show-config | grep -i cloud
   # api_key must be "***"; the literal credential must not appear.
   ```

4. **Dispatch one Cloud run only if the exact Agent Profile UUID points to a
   model you accept using.** Configure that profile in OpenHands Cloud first,
   leave `OPENHANDS_CLOUD_MODEL` unset, set `OPENHANDS_CLOUD_PROFILE` to the
   profile UUID, keep the quality gates configured, and dispatch a single task
   against the matching target repository on an isolated factory branch.

   - The factory uploads a bundle from the local isolated worktree and verifies
     the exact base SHA and branch before the conversation starts.
   - The conversation receives no GitHub token or secret.
   - On success the factory downloads a result bundle, verifies its branch and
     SHA locally, then runs local gates. The agent never pushes.
   - Watch `--show-config` and the logs: no credential may appear.
   - On success, confirm the run reaches `VALIDATING`, gates run **locally**, and
     the run stops at `WAITING_HUMAN` after a PR — or fails closed with `FAILED`
     and no PR if the revision could not be validated.

5. **Confirm idempotent recovery.** Re-run collection for the same run: no second
   sandbox or conversation is created (the run re-derives both from
   `provider_ref`). A retry is a brand new `AgentRun` on a new branch.

Do not purchase credits, change billing, or automate browser login as part of the
smoke test.

## What this task deliberately does not do

- It does not switch production/default execution to Cloud.
- It does not auto-merge, deploy, or modify hosts, containers or secrets.
- It does not weaken local revision validation or quality gates.
- It does not modify Finanza IA.
