# OpenHands Cloud backend — operator runbook

This runbook covers running the factory with the **OpenHands Cloud** execution
backend. Cloud is optional and never the default; local mode is unaffected by
anything here.

## What Cloud changes, and what it does not

A Cloud run executes the agent on an OpenHands Cloud sandbox instead of the
self-hosted Agent Server. Everything after execution is unchanged:

```
dispatch ──► Cloud sandbox conversation ──► collect
                                             │  terminal success only
                                             ▼
                        read head commit from the sandbox git contract
                                             ▼
                        fetch that exact commit into a fresh LOCAL worktree
                                             ▼
                        quality gates run locally (VALIDATING)
                                             ▼
                        validated revision binding (Phase 4)
                                             ▼
                        publication (Phase 5) — PR, then WAITING_HUMAN
```

The factory **never publishes a Cloud result on the sandbox's word**. If the
exact revision cannot be retrieved and re-fetched locally, the run is failed
closed and no pull request is opened. The factory always stops at
`WAITING_HUMAN`; it never merges.

## Configuration

Set `OPENHANDS_BACKEND=cloud` plus the Cloud settings. All values are optional
until Cloud is selected, and none is logged or committed (see `.env.example`):

| Variable | Purpose |
|---|---|
| `OPENHANDS_BACKEND` | `local` (default) or `cloud`. |
| `OPENHANDS_CLOUD_API_KEY` | Cloud API bearer credential. Required for Cloud. |
| `OPENHANDS_CLOUD_REPOSITORY` | `owner/name` repository Cloud works in. Required for Cloud. |
| `OPENHANDS_CLOUD_API_URL` | Cloud API base URL. Default `https://app.all-hands.dev`. |
| `OPENHANDS_CLOUD_WORKING_DIR` | Sandbox directory the repository is checked out at. |
| `OPENHANDS_CLOUD_BASE_REF` | Optional commit-ish the factory branch starts from. |
| `OPENHANDS_CLOUD_PROFILE` / `OPENHANDS_CLOUD_MODEL` | LLM configuration Cloud uses. |

Credentials come from the environment or a secret store. The factory never
handles an LLM key when an agent profile is used: the sandbox resolves it.

### Model / profile selection is configuration, never a free-model dependency

There is no hard-coded model and no automatic paid-model fallback. If the
configured profile or model is unavailable, the run fails with a sanitized
provider error. Pick a profile/model deliberately in your Cloud account and set
it; do not rely on any particular model being free or unlimited.

### Failing closed

- `OPENHANDS_BACKEND=cloud` with a missing key or repository → the factory
  refuses to start with a `BackendConfigurationError`. It does **not** run local
  instead.
- A Cloud dispatch/collect failure → a sanitized error; the run follows the
  existing failure path. There is no local↔cloud fallback.
- A terminal success whose revision cannot be retrieved or re-fetched → the run
  is `FAILED`; nothing is published.

### Redaction check

```
python -m factory --show-config
```

The Cloud API key must appear as `"***"` (or `null` when unset), and the bearer
key must never appear in any log line, error message or URL.

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

4. **Dispatch one Cloud run only if your configured profile is one you accept
   using.** Set `OPENHANDS_CLOUD_PROFILE` to a profile you have chosen (for
   example, a free-tier profile), keep the quality gates configured, and dispatch
   a single task against the target repository on an isolated factory branch.

   - The factory creates one sandbox and one conversation; it never pushes to
     `main`/`master`.
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
