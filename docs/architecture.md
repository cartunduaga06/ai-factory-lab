# Architecture

AI Factory Lab is the control plane of a software-development factory. It turns
work items into verified Pull Requests produced by autonomous coding agents, and
stops short of merging them.

Two repositories are involved and must stay separate:

- **`ai-factory-lab`** — orchestration / control plane (this repository).
- **`finanza-ia`** — product repository / execution target (never modified
  outside an agent's assigned task and isolated branch).

## System diagram

```
                        Human / Dashboard / API
                                  │
                                  ▼
                     ┌────────────────────────┐
                     │  AI Factory Orchestrator│
                     │  (factory.orchestration)│
                     └───────────┬────────────┘
                                 │
                                 ▼
                     ┌────────────────────────┐
                     │   Task / Issue Queue   │
                     │  FactoryTask (typed)   │
                     │  source: GitHub Issues │
                     └───────────┬────────────┘
                                 │
                                 ▼
                     ┌────────────────────────┐
                     │    Agent Dispatcher    │
                     │  AgentAdapter protocol │
                     └─────┬────────────┬─────┘
                           │            │
              ┌────────────▼──┐   ┌─────▼──────────┐
              │   OpenHands   │   │     Codex      │   ...future engines
              └───────┬───────┘   └───────┬────────┘
                      └────────┬──────────┘
                               ▼
                ┌──────────────────────────────┐
                │  isolated workspace / branch │
                │  Workspace (per run)         │
                └──────────────┬───────────────┘
                               ▼
                ┌──────────────────────────────┐
                │        implementation        │
                │        AgentRun              │
                └──────────────┬───────────────┘
                               ▼
                ┌──────────────────────────────┐
                │       tests / QA gates       │
                │       QualityGate[]          │
                └──────────────┬───────────────┘
                               ▼
                     ┌──────────────────┐
                     │   Pull Request   │
                     └────────┬─────────┘
                              ▼
                     ┌──────────────────┐
                     │  human approval  │   ◄── mandatory gate
                     └────────┬─────────┘
                              ▼
                           merge
                    (performed by a human)
```

## Component / layer diagram

```
┌───────────────────────────────────────────────────────────────┐
│                            domain                             │
│  FactoryTask · TaskSource · TaskTransition · AgentRun ·       │
│  Repository · Workspace · PullRequest · QualityGate ·         │
│  AgentAdapter (protocol) · IssueSource/TaskRepository (ports) │
│  pure data + invariants — no I/O                              │
└───────────────────────────▲───────────────────────────────────┘
                            │ depends on
┌───────────────────────────┴───────────────────────────────────┐
│                        orchestration                          │
│  TaskStateMachine · lifecycle table · IssueIntakeService ·    │
│  TaskLifecycleService · DispatchService · RunTrackingService  │
│  depends on ports and the lifecycle, never a concrete engine, │
│  GitHub client, SQLite implementation, git or subprocess      │
└───────────────────────────▲───────────────────────────────────┘
                            │ depends on
┌───────────────────────────┴───────────────────────────────────┐
│                        integrations                           │
│  GitHub: GitHubClient (read-only) · GitHubIssueSource         │
│  OpenHands: OpenHandsClient · OpenHandsExecution · status     │
│  mapping · OpenHandsAdapter (AgentAdapter implementation)     │
│  Workspaces: GitWorktreeWorkspaceProvisioner (git worktree)   │
│  Gates: LocalQualityGateRunner (argv, no shell, bounded)      │
│  PullRequestSink · AgentAdapterBase                           │
└───────────────────────────▲───────────────────────────────────┘
                            │ depends on
┌───────────────────────────┴───────────────────────────────────┐
│                      infrastructure                           │
│  FactoryConfig (env) · logging · SqliteTaskRepository ·       │
│  SqliteRunRepository                                          │
└───────────────────────────────────────────────────────────────┘
```

Dependencies point inward. Only `infrastructure` and `integrations` touch the
outside world. `orchestration` knows GitHub and SQLite only as the `IssueSource`,
`TaskRepository` and `RunRepository` ports declared in `domain/ports.py`.

## Package layout

```
src/factory/
├── __init__.py              # public surface
├── __main__.py              # `python -m factory` config check + `intake`
├── domain/
│   ├── enums.py             # TaskStatus, RunStatus, QualityGateStatus, ...
│   ├── models.py            # dataclasses + AgentAdapter protocol + TaskSource
│   ├── errors.py            # domain errors (duplicate source, lost update, dispatch)
│   └── ports.py             # IssueSource, TaskRepository, RunRepository +
│                            #   WorkspaceProvisioner, QualityGateRunner
├── orchestration/
│   ├── lifecycle.py         # transition table (single source of truth)
│   ├── machine.py           # TaskStateMachine, InvalidTransitionError
│   ├── intake.py            # IssueIntakeService (provider-agnostic)
│   ├── dispatch.py          # DispatchService (claim + workspace + run)
│   ├── tracking.py          # RunTrackingService (collect → validate → lifecycle)
│   └── transitions.py       # TaskLifecycleService (state machine + persistence)
├── integrations/
│   ├── base.py              # PullRequestSink, AgentAdapterBase
│   ├── gates/
│   │   └── local.py         # LocalQualityGateRunner (argv, no shell, bounded)
│   ├── workspace/
│   │   └── git.py           # GitWorktreeWorkspaceProvisioner (worktree per run)
│   ├── github/
│   │   ├── client.py        # read-only REST client, injectable transport
│   │   └── issues.py        # GitHubIssueSource (IssueSource implementation)
│   └── openhands/
│       ├── client.py        # Agent Server HTTP client, injectable transport
│       ├── execution.py     # task -> conversation request + instruction
│       ├── status.py        # execution_status -> RunStatus (single source)
│       └── adapter.py       # OpenHandsAdapter (AgentAdapter implementation)
└── infrastructure/
    ├── config.py            # FactoryConfig.from_env + redaction + DatabaseConfig
    ├── logging.py           # configure_logging
    └── persistence/
        ├── schema.py        # idempotent SQLite DDL
        ├── codec.py         # shared datetime encoding
        ├── sqlite_base.py   # shared connection + idempotent initialize
        ├── sqlite.py        # SqliteTaskRepository (TaskRepository implementation)
        └── run_sqlite.py    # SqliteRunRepository (RunRepository implementation)
```

## Domain model

The seven concepts from the specification, and where they are defined:

| Concept | Definition | Notes |
|---|---|---|
| `FactoryTask` | `domain/models.py` | `source: TaskSource` gives structured identity; `external_ref` is derived for display only. |
| `TaskSource` | `domain/models.py` | Frozen identity triple `(provider, repository_slug, issue_number)`. |
| `TaskTransition` | `domain/models.py` | Frozen audit record of one `from_status → to_status` change. |
| `AgentRun` | `domain/models.py` | One attempt; carries `adapter: AgentKind`, its `Workspace` and `QualityGate[]`. |
| `Repository` | `domain/models.py` | `slug` + `RepositoryRole` (`CONTROL_PLANE` / `TARGET`). |
| `Workspace` | `domain/models.py` | Ephemeral, per-run, must have a branch. |
| `PullRequest` | `domain/models.py` | Records the proposal; merge is external and human. |
| `QualityGate` | `domain/models.py` | Named check with `QualityGateStatus`; required gates block. |
| `AgentAdapter` | `domain/models.py` | `runtime_checkable` `Protocol` — the engine seam. |
| `IssueSource` | `domain/ports.py` | Port: supplies work items. Read-only in Phase 2A. |
| `TaskRepository` | `domain/ports.py` | Port: persists tasks and transition history. |

Design intent:

- **Engine independence.** Orchestration sees only `AgentAdapter.kind`,
  `dispatch`, `collect` and `cancel`. Adding Codex or any other engine is a new
  integration, not an orchestration change.
- **Structured source identity.** `FactoryTask.source` is a `TaskSource`, not a
  parsed string. The triple `(provider, repository_slug, issue_number)` is the
  deterministic uniqueness key; `external_ref` exists for display and backward
  compatibility but is never authoritative.
- **Ports, not implementations.** The domain declares `IssueSource`,
  `TaskRepository` and `RunRepository`; GitHub lives in `integrations`, SQLite in
  `infrastructure`.
- **Immutable value types.** `Repository`, `Workspace`, `PullRequest`,
  `QualityGate`, `TaskSource` and `TaskTransition` are frozen; `FactoryTask` and
  `AgentRun` are mutable because the lifecycle advances them.

## Task state machine

```
                              ┌──────────────┐
                              │  DISCOVERED  │
                              └──────┬───────┘
                                     │
                              ┌──────▼───────┐
                              │    READY     │◄──────────────┐
                              └──────┬───────┘               │
                                     │                       │
                              ┌──────▼───────┐        ┌──────┴───────┐
                              │   CLAIMED    │        │   BLOCKED    │
                              └──────┬───────┘        └──────▲───────┘
                                     │                       │
                              ┌──────▼───────┐               │
                              │   RUNNING    │───────────────┘
                              └──────┬───────┘
                                     │
                              ┌──────▼───────┐
                              │  VALIDATING  │──┐
                              └──────┬───────┘  │ gates failed → READY
                                     │          │
                              ┌──────▼───────┐  │
                              │   PR_OPEN    │  │
                              └──────┬───────┘  │
                                     │          │
                              ┌──────▼────────┐ │
                              │ WAITING_HUMAN │ │
                              └──────┬────────┘ │
                                     │          │
                              ┌──────▼───────┐  │
                              │     DONE     │  │
                              └──────────────┘  │
                                                │
   Any non-terminal state ──────────────────────┘
     may also move to FAILED or CANCELLED (terminal sinks)
```

Transition table (authoritative: `factory/orchestration/lifecycle.py`):

| From | Allowed next |
|---|---|
| `DISCOVERED` | `READY`, `CANCELLED`, `FAILED` |
| `READY` | `CLAIMED`, `BLOCKED`, `CANCELLED` |
| `CLAIMED` | `RUNNING`, `BLOCKED`, `CANCELLED` |
| `RUNNING` | `VALIDATING`, `BLOCKED`, `FAILED`, `CANCELLED` |
| `VALIDATING` | `PR_OPEN`, `READY`, `FAILED`, `CANCELLED` |
| `PR_OPEN` | `WAITING_HUMAN`, `FAILED`, `CANCELLED` |
| `WAITING_HUMAN` | `DONE`, `FAILED`, `CANCELLED` |
| `BLOCKED` | `READY`, `CANCELLED` |
| `DONE` / `FAILED` / `CANCELLED` | — (terminal) |

Two deliberate choices:

- **`VALIDATING → READY`** is the retry edge: a failed gate sends the task back
  for another run instead of opening a bad PR.
- **`BLOCKED`** is the only non-terminal failure state, because a blocker (missing
  information, a dependency) is usually removable. It re-enters at `READY`.

No workflow engine is implemented. The machine stays pure: it validates
transitions, and `TaskLifecycleService` is the thin orchestration seam that
applies a validated transition and records it in persistence atomically.

## Issue intake (Phase 2A)

The first operational control-plane capability is intake:

```
GitHub Issue
      ↓
GitHubIssueSource      (integrations — read-only REST)
      ↓
FactoryTask            (domain — with structured TaskSource)
      ↓
IssueIntakeService     (orchestration — provider-agnostic)
      ↓
TaskRepository         (domain port)
      ↓
SQLite                 (infrastructure)
      ↓
Transition History     (transitions table)
```

### Eligibility

An issue is eligible when it is **open**, carries the `factory-ready` label, and
is **not** a pull request. The factory observes GitHub only — intake never adds,
removes or changes labels, comments, state or any other GitHub resource.

### Structured source identity

`TaskSource(provider, repository_slug, issue_number)` is the deterministic
identity of a task. `github` + `cartunduaga06/ai-factory-lab` + `42` identifies
exactly one task. `FactoryTask.external_ref` (`...#42`) is derived for display
and is never used as the identity key.

### Idempotency

Intake checks `TaskRepository.find_by_source()` before writing and skips tasks
that already exist, leaving them untouched. The `tasks` table additionally
enforces `UNIQUE(source_provider, source_repository, source_issue_number)` as a
defense-in-depth guarantee: a concurrent writer that slips past the check still
cannot create a duplicate.

### Transition persistence

`TaskLifecycleService.transition()` retrieves the task, validates the requested
transition with the pure `TaskStateMachine`, then applies the status change and
inserts the history row **in one transaction**. If either half fails, neither is
committed: the status stays put and no history row appears. An invalid
transition raises before any write occurs.

## Configuration model

`FactoryConfig.from_env()` reads environment variables only, groups them by
integration (`github`, `openhands`, `codex`, database, logging) and defaults
safely so the repository runs with nothing configured:

- Missing or empty values → `None`, never a real default credential.
- Unresolved `<placeholder>` values (from `.env.example`) are treated as unset,
  so a copied example file cannot masquerade as configuration.
- `DATABASE_URL` defaults to `sqlite:///./factory.db`. SQLite is the only
  supported backend; a non-SQLite scheme raises `UnsupportedDatabaseError` when
  the URL is parsed. Parsing is deferred until the database is actually needed,
  so loading configuration never fails on an unusable URL.
- `FactoryConfig.redacted()` is the only supported way to log configuration.

See `.env.example` for the full reference.

## Run lifecycle and dispatch (Phase 2B)

Dispatch turns a ready task into a durable run:

```
FactoryTask (READY)
      ↓
DispatchService        (orchestration — depends on AgentAdapter only)
      ├──────────────► AgentAdapter.dispatch(task, workspace)
      ↓
READY → CLAIMED         (atomic compare-and-swap, history row committed with it)
      ↓
Workspace + AgentRun   (domain)
      ↓
RunRepository          (domain port)
      ↓
SQLite                 (infrastructure — workspaces + agent_runs tables)
```

### Claim atomicity

The `READY → CLAIMED` claim goes through the same conditional-update
compare-and-swap as every other transition. Two dispatchers cannot both win: the
loser's guarded update matches zero rows and is refused. The loser surfaces a
`DispatchConflictError` and creates no workspace and no run.

### Idempotency

The rule is: **a task that already has an active run has already been
dispatched.** Because dispatch does not advance the task past `CLAIMED`, such a
task fails the `READY` requirement on a retry and the original run is preserved.
The rule is enforced twice:

1. `DispatchService` requires `READY` before claiming.
2. `agent_runs` carries a partial unique index, `uq_agent_runs_active_task`,
   allowing at most one non-terminal (`PENDING`, `RUNNING`) run per task.

Terminal runs fall outside the partial index, so a retry after a finished run
remains possible.

### Adapter boundary

`DispatchService` reads only `AgentAdapter.kind` and calls `dispatch`. No engine
is named in orchestration; Phase 2B ships no concrete adapter, and tests use a
deterministic fake. An adapter failure is normalized into `AgentDispatchError`
built from sanitized factory data only. The engine's own exception is discarded
at this boundary rather than chained: retaining it as `__cause__`/`__context__`
would let Python render it in the traceback, so a token in the engine's message
could still reach factory logs or CLI output. The `except` block captures
nothing and exits before the error is raised, so the resulting
`AgentDispatchError` has no cause or context and its formatted traceback carries
no engine text. The failed attempt is still recorded as a terminal `FAILED` run
so it stays auditable.

## OpenHands adapter (Phase 3)

OpenHands is the factory's first real *execution engine*. It stays on the
outside of the architecture: the factory owns intake, task, dispatch, run
tracking and lifecycle; OpenHands only runs the implementation step.

```
FactoryTask + Workspace            (domain)
      ↓  DispatchService           (orchestration — AgentAdapter only)
      ↓  AgentAdapter.dispatch     (protocol)
      ↓  OpenHandsAdapter          (integrations/openhands)
      ↓  OpenHandsClient           (HTTP, injectable transport)
      ↓  POST /api/conversations   (OpenHands Agent Server)
   conversation id ──────────────► AgentRun.run_id
```

### Detected interface

The adapter targets the OpenHands **Agent Server v1** HTTP API. On the machine
used for this phase that is a local server (v1.49.5) exposing `/health`,
`/ready`, `/server_info` and `/api/*`, with request authentication through the
`X-Session-API-Key` header. The factory does not bundle or import the OpenHands
SDK; it speaks HTTP through a small injectable transport, so the integration has
no new runtime dependency and is fully testable offline.

### Run identity and status mapping

The OpenHands conversation id *is* `AgentRun.run_id` — one-to-one, with no
provider-specific domain field. Engine execution states are normalized by
`integrations/openhands/status.py`, which is the single source of truth:

| OpenHands `execution_status` | Factory `RunStatus` |
|---|---|
| `idle` | `PENDING` |
| `running`, `paused`, `waiting_for_confirmation` | `RUNNING` |
| `finished` | `SUCCEEDED` |
| `error`, `stuck` | `FAILED` |
| `deleting` | `CANCELLED` |

An unrecognized state raises `OpenHandsStatusError`. It is never guessed — in
particular never mapped to `SUCCEEDED`.

### Cancel

`cancel` requests an interrupt on the conversation. It is idempotent: a run that
is already terminal from the factory's point of view is left alone, and a
conversation that no longer exists (`404`) is treated as already cancelled. Any
other API failure is raised, never converted into success.

### Credentials

The adapter prefers a **server-side agent profile** (`OPENHANDS_AGENT_PROFILE_ID`):
the agent server resolves the LLM and its credential itself, so the factory never
holds an LLM key. The session key (`OPENHANDS_SESSION_API_KEY`) authenticates
factory→server requests and is distinct from any LLM credential. `--show-config`
redacts both. No token, header, raw response body or credential-bearing URL can
leave the integration: failures are translated to sanitized errors that carry no
cause or context, matching the Phase 2B boundary. Remote HTTP error-body text
(`detail`, `message`, `error`, ...) is discarded at the client boundary rather
than partially redacted — the client cannot know every credential a server or LLM
provider might echo — so only the trusted numeric status leaves the client.

## Isolated workspaces and validation (Phase 4)

Phase 4 makes dispatch operate inside a physical, per-run Git workspace and adds
the validation lifecycle that runs quality gates after a successful agent run.

```
FactoryTask (READY)
      ↓
DispatchService                     (orchestration — ports only)
      ├─ READY → CLAIMED             (atomic compare-and-swap)
      ├─ new_workspace(task, root)   (domain — pure identity)
      ├─ WorkspaceProvisioner.prepare ──► git worktree add (integrations)
      ├─ AgentAdapter.dispatch(task, workspace)
      ├─ RunRepository.save_run
      └─ CLAIMED → RUNNING
```

### One workspace and branch per run attempt

A workspace is keyed by its **own generated id**, not the task id:

```
branch: factory/<task-id>/<workspace-id>
path:   <workspace-root>/<workspace-id>
```

The id is created before the engine is involved (the OpenHands conversation id
is not known until dispatch), and it is what makes retries safe: a second attempt
at the same task gets a fresh workspace id and therefore a different branch and
path. This is the guardrail — **one unique branch and one unique working tree per
run attempt**.

`new_workspace()` is pure: it computes the identity and nothing else. The
physical checkout is created by the injected
`WorkspaceProvisioner`, so neither the domain nor orchestration touches the
filesystem or git.

The invariant is enforced at the persistence boundary too, not only during
dispatch. A persisted run's workspace association is immutable: `update_run()`
cannot move a run to another workspace or clear one, and a partial unique index
(`uq_agent_runs_workspace`) refuses two runs that point at the same non-null
`workspace_id`. Even if an application-level check were bypassed, storage rejects
the duplicate. Runs without a workspace are exempt, so the guard never affects a
run that never had one.

### The Git worktree provisioner

`integrations/workspace/git.py` implements the port with `git worktree`:

- the **source checkout stays on its existing branch** — only `worktree add` runs,
  and the factory branch is never checked out there;
- no `fetch`, `push`, `reset`, checkout of a branch in the source tree, or any
  history rewrite; the command surface is deliberately tiny;
- preparation is **retry-safe**: an existing, matching workspace is a no-op; a
  pre-existing directory or a worktree on the wrong branch is **refused**, never
  silently reused;
- failures are normalized to a sanitized `WorkspaceProvisioningError`; git's
  stderr — which can echo a remote URL with an embedded token — is discarded
  rather than chained into the error;
- the source checkout location is **injected** (`FACTORY_SOURCE_CHECKOUT`), never
  a hard-coded machine path.

### Quality gate semantics

`QualityGate.is_green` is true only when the status is exactly `PASSED`. For a
**required** gate, `PENDING`, `FAILED` and `SKIPPED` are all not green — an
unevaluated or skipped requirement is never counted as satisfied. An optional
gate never blocks, whatever its status.

`AgentRun.required_gates_passed` asks the required-only question;
`AgentRun.validation_outcome` turns it into a deterministic
`ValidationOutcome`:

| Outcome | Meaning |
|---|---|
| `PENDING` | The run has not reached terminal success. |
| `READY_FOR_NEXT_PHASE` | Every required gate passed. |
| `GATES_FAILED` | At least one required gate did not pass. |

**Zero configured gates** yields `READY_FOR_NEXT_PHASE` (vacuously true): the
factory does not invent gates a repository never defined, and nothing was
configured to block the run. Callers that must distinguish this use
`AgentRun.has_required_gates`.

### Run tracking and validation lifecycle

`RunTrackingService` (orchestration) refreshes an active run through
`AgentAdapter.collect()` and drives the task from the normalized run status. It
depends only on `AgentAdapter` and the `QualityGateRunner` port — never a
concrete engine or runner, and never on `AgentKind`.

| Engine run status | Task effect |
|---|---|
| `PENDING` / `RUNNING` | Task stays `RUNNING`. |
| `SUCCEEDED` | Task `RUNNING → VALIDATING`, then gates run in the workspace and results are persisted on the run. |
| `FAILED` | Task moves through the existing legal failure edge. |
| `CANCELLED` | Task moves through the existing legal cancellation edge. |

A terminal run already in storage is **not re-collected** on a later refresh and
its gates are **not re-run**, but the task lifecycle is still reconciled from it.
This closes the crash window between persisting the terminal run and applying the
matching task transition: if the process died in between, the run is terminal in
storage while the task is still `RUNNING`. Reconciliation re-applies the
transition, and re-running it is a no-op once the task is already in the target
state, so repeated refreshes add no transition history, no gate executions and
no run writes.

Two guards make reconciliation safe and deterministic:

- only the task's **latest** run may drive the lifecycle, so a superseded run
  from an earlier attempt never rewinds a task that has since been retried;
- a `SUCCEEDED` run with no persisted gates while gate specs *are* configured is
  evaluated once and its gates persisted (the one recovery case). With no gate
  specs configured the factory invents nothing and only reconciles.

`CLAIMED → RUNNING` happens only *after* the run is durable, so a task is never
`RUNNING` without a run behind it.

### Collection security boundary

`AgentAdapter.collect()` is called under the same boundary as `dispatch`. Run
tracking is engine-agnostic and cannot assume an adapter sanitizes its own
errors, so a raw engine failure is discarded at this seam rather than chained:

```
AgentAdapter.collect()  ──►  raw engine failure discarded
                    └──►  AgentCollectError(run_id, task_id)
```

`AgentCollectError` is built from factory-domain identifiers only. The adapter's
exception is not retained as `__cause__` or `__context__` — chaining it would let
Python render it in the traceback, and a token or credential-bearing URL in the
engine's message could then reach factory logs or the CLI. Nothing is persisted
when collection fails: the stored run keeps its previous non-terminal status and
the task stays `RUNNING`, so a later refresh simply retries collection. The
terminal reconciliation path above is unaffected — it does not call `collect`.

### Validation behaviour, and the Phase 4 stop line

When the agent succeeds the task reaches `VALIDATING` and gates are attached to
the `AgentRun` and persisted. If every **required** gate is `PASSED` the
validation result is green (`READY_FOR_NEXT_PHASE`) — **but the task remains
`VALIDATING`.** `PR_OPEN` is deliberately not reached: no pull request exists
yet, and opening one belongs to Phase 5. A failed required gate also leaves the
task `VALIDATING`, so it never advances toward a PR. Phase 4 never auto-dispatches
a correction; it exposes the deterministic outcome so later orchestration can
decide.

### The run update operation

`RunRepository.update_run()` updates a run that is already stored. It is **not an
upsert**: an unknown run is refused with `KeyError`; `run_id`, task identity and
the workspace association are immutable, and no second row is created — so the
one-active-run invariant is untouched. It persists `status`, `summary`,
`started_at`, `finished_at` and `gates`, durably across a repository reopen. A
caller that tries to move the run to a different workspace is refused with a
sanitized `PersistenceError` whose message names only the run id.

### The local quality gate runner

`integrations/gates/local.py` implements the `QualityGateRunner` port. Gates
come from `QualityGateSpec` as an **argv tuple**, and execution is
`subprocess.run(..., shell=False)` with:

- `cwd` set to `Workspace.path`;
- a bounded timeout (a timeout is a `FAILED` gate, never a hung factory);
- a minimal environment allowlist, so factory credentials are not handed to the
  command;
- a sanitized `detail` only — `exit_code=0`, `exit_code=1`, `timeout` or
  `spawn_error`. Raw stdout/stderr are **never persisted** in MVP 0.1.

Which gates exist is supplied by the application layer through
`FACTORY_QUALITY_GATES` (a JSON array of `{name, argv, required}`); neither the
domain nor orchestration hard-codes a command.

## Planned evolution

| Phase | Addition |
|---|---|
| 2A | ✅ GitHub Issue intake + SQLite task/transition persistence |
| 2B | ✅ Run lifecycle persistence (`AgentRun`, `Workspace`) + `DispatchService` |
| 3 | ✅ OpenHands `AgentAdapter` (dispatch, collect, cancel) |
| 4 | ✅ Isolated Git worktree workspace per run; quality-gate validation in `VALIDATING` |
| 5 | PR creation, `WAITING_HUMAN` handoff; Codex adapter |
| 6 | API / dashboard over the orchestrator |
