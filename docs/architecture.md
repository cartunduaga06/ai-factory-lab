# Architecture

The E3 sprint controller is an optional authorization gate around the existing
E2 materializer and one-shot GitHub runtime. With Trello backlog configuration,
an immutable SQLite manifest fixes the ordered WorkItems and WIP=1 policy.
Only the current linked Issue may be selected by `run` or `watch`; no authorized
manifest means no selection. The controller pauses on `WAITING_HUMAN`, `BLOCKED`
and `FAILED`, and resumes only after an explicit human command and resolution
of the task gate. Sprint facts are appended to the E1 trace, keyed by
`sprint:<sprint_id>`. See [backlog.md](backlog.md) for the operator commands.

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
│  Repository · Workspace · PullRequest · PublishedRevision ·   │
│  QualityGate · AgentAdapter (protocol) · IssueSource/         │
│  TaskRepository/RunRepository/WorkspaceProvisioner/           │
│  QualityGateRunner/WorkspacePublisher/PullRequestSink/        │
│  WorkspaceRevisionInspector/PullRequestRepository (ports)     │
│  pure data + invariants — no I/O                              │
└───────────────────────────▲───────────────────────────────────┘
                            │ depends on
┌───────────────────────────┴───────────────────────────────────┐
│                        orchestration                          │
│  TaskStateMachine · lifecycle table · IssueIntakeService ·    │
│  TaskLifecycleService · DispatchService · RunTrackingService  │
│  PublicationService                                           │
│  depends on ports and the lifecycle, never a concrete engine, │
│  GitHub client, SQLite implementation, git or subprocess      │
└───────────────────────────▲───────────────────────────────────┘
                            │ depends on
┌───────────────────────────┴───────────────────────────────────┐
│                        integrations                           │
│  GitHub: GitHubClient (read-only) · GitHubIssueSource         │
│  GitHub: GitHubWriteClient · GitHubPullRequestSink (Phase 5)  │
│  OpenHands: OpenHandsClient · OpenHandsExecution · status     │
│  mapping · OpenHandsAdapter (AgentAdapter implementation)     │
│  Codex: CodexAdapter (bounded local CLI execution)            │
│  Workspaces: GitWorktreeWorkspaceProvisioner (git worktree)   │
│  Workspaces: GitWorkspacePublisher (commit + secure push)     │
│  Gates: LocalQualityGateRunner (argv, no shell, bounded)      │
│  AgentAdapterBase                                             │
└───────────────────────────▲───────────────────────────────────┘
                            │ depends on
┌───────────────────────────┴───────────────────────────────────┐
│                      infrastructure                           │
│  FactoryConfig (env) · logging · SqliteTaskRepository ·       │
│  SqliteRunRepository · SqlitePullRequestRepository            │
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
│   ├── errors.py            # domain errors (duplicate source, lost update, dispatch,
│   │                        #   publication, unsafe remote, duplicate PR)
│   └── ports.py             # IssueSource, TaskRepository, RunRepository +
│                            #   WorkspaceProvisioner, QualityGateRunner +
│                            #   WorkspacePublisher, WorkspaceRevisionInspector,
│                            #   PullRequestSink, PullRequestRepository
├── orchestration/
│   ├── lifecycle.py         # transition table (single source of truth)
│   ├── machine.py           # TaskStateMachine, InvalidTransitionError
│   ├── intake.py            # IssueIntakeService (provider-agnostic)
│   ├── dispatch.py          # DispatchService (claim + workspace + run)
│   ├── tracking.py          # RunTrackingService (collect → validate → lifecycle)
│   ├── publication.py       # PublicationService (publish → PR → WAITING_HUMAN)
│   ├── runtime.py           # FactoryRuntime (one-shot: intake → … → WAITING_HUMAN)
│   ├── watch.py             # FactoryWatcher (sequential one-task loop, WIP=1)
│   └── transitions.py       # TaskLifecycleService (state machine + persistence)
├── integrations/
│   ├── base.py              # AgentAdapterBase; re-exports the domain ports
│   ├── gates/
│   │   └── local.py         # LocalQualityGateRunner (argv, no shell, bounded)
│   ├── workspace/
│   │   ├── git.py           # GitWorktreeWorkspaceProvisioner (worktree per run)
│   │   ├── shared_policy.py # cross-UID permission policy + OpenHands hook CLI
│   │   └── git_publish.py   # GitWorkspacePublisher (commit + secure push)
│   ├── github/
│   │   ├── client.py        # read-only REST client, injectable transport
│   │   ├── issues.py        # GitHubIssueSource (IssueSource implementation)
│   │   ├── write_client.py  # write-capable REST client, sanitized errors
│   │   └── pull_requests.py # GitHubPullRequestSink (find/open only, never merge)
│   ├── codex/
│   │   └── adapter.py       # CodexAdapter (noninteractive CLI)
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
        ├── run_sqlite.py    # SqliteRunRepository (RunRepository implementation)
        └── pr_sqlite.py     # SqlitePullRequestRepository (PullRequestRepository)
```

## Domain model

The seven concepts from the specification, and where they are defined:

| Concept | Definition | Notes |
|---|---|---|
| `FactoryTask` | `domain/models.py` | `source: TaskSource` gives structured identity; `external_ref` is derived for display only. |
| `TaskSource` | `domain/models.py` | Frozen identity triple `(provider, repository_slug, issue_number)`. |
| `TaskTransition` | `domain/models.py` | Frozen audit record of one `from_status → to_status` change. |
| `AgentRun` | `domain/models.py` | One attempt; carries `adapter: AgentKind`, its `Workspace`, `QualityGate[]` and the bound `validated_revision`. |
| `WorkspaceRevisionInspector` | `domain/ports.py` | Binds/verifies an opaque identity of a workspace's publishable state; concrete Git implementation in `integrations/workspace/revision.py`. |
| `Repository` | `domain/models.py` | `slug` + `RepositoryRole` (`CONTROL_PLANE` / `TARGET`). |
| `Workspace` | `domain/models.py` | Ephemeral, per-run, must have a branch. |
| `PullRequest` | `domain/models.py` | Records the proposal; merge is external and human. |
| `QualityGate` | `domain/models.py` | Named check with `QualityGateStatus`; required gates block. |
| `PublishedRevision` | `domain/models.py` | Frozen `(commit_sha, branch)` identity of a published workspace revision. |
| `AgentAdapter` | `domain/models.py` | `runtime_checkable` `Protocol` — the engine seam. |
| `IssueSource` | `domain/ports.py` | Port: supplies work items. Read-only in Phase 2A. |
| `TaskRepository` | `domain/ports.py` | Port: persists tasks and transition history. |
| `WorkspacePublisher` | `domain/ports.py` | Port: commits a workspace and pushes its isolated branch. |
| `PullRequestSink` | `domain/ports.py` | Port: find/open pull requests. Deliberately has no merge. |
| `PullRequestRepository` | `domain/ports.py` | Port: durable storage of the PRs the factory opens. |

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
| `VALIDATING` | `PR_OPEN`, `READY`, `BLOCKED`, `DONE`, `FAILED`, `CANCELLED` |
| `PR_OPEN` | `WAITING_HUMAN`, `FAILED`, `CANCELLED` |
| `WAITING_HUMAN` | `CHANGES_REQUESTED`, `DONE`, `FAILED`, `CANCELLED` |
| `CHANGES_REQUESTED` | `READY`, `CANCELLED` |
| `BLOCKED` | `READY`, `CANCELLED` |
| `DONE` / `FAILED` / `CANCELLED` | — (terminal) |

The Control Tower presents `STALLED` when a non-terminal run in `RUNNING` has
missed the configured number of persisted heartbeats. This is a read-only
observation, not a `TaskStatus` transition, so a later successful collection
recovers the display to `RUNNING` without rewriting lifecycle history. The
SQLite transition trigger records meaningful state changes in a durable status
outbox. The orchestration status service projects only allowlisted identifiers,
times, links and deterministic evidence; the Trello adapter consumes that same
projection. Heartbeat writes create no transition event.

`VALIDATING → DONE` is reserved for accepted `OPERATIONAL` scratch tasks;
`VALIDATING → BLOCKED` records failed operational acceptance. `CODE` tasks
continue through `PR_OPEN → WAITING_HUMAN`. The operational declaration,
capability and gates are described in [operational-scratch.md](operational-scratch.md).

For CODE tasks at `WAITING_HUMAN`, each runtime pass reads the persisted PR by
number through the read-only GitHub client. Only an exact repository, head,
base, and number match can advance it: a confirmed merge moves it to `DONE`,
and a confirmed close without merge moves it to `CANCELLED`. Open or uncertain
responses leave it at `WAITING_HUMAN`; read errors stop the pass before new work.
The transition is durable and idempotent. The factory never merges or closes a PR.

Human QA can request another code pass with
`python -m factory request-changes --task-id <uuid> --feedback-file <path>`.
The command verifies that the task is awaiting review, its persisted PR identity
matches the reviewed run, and GitHub still reports that PR open. It rejects
empty, oversized, control-character and credential-like feedback. SQLite stores
each accepted request with its reviewed run and atomically records
`WAITING_HUMAN → CHANGES_REQUESTED`; a repeated request cannot create another
cycle. The next runtime pass advances to `READY`, dispatches Codex in the
reviewed checkout, validates a new run, then verifies the same open PR before
pushing the same branch. The task returns to `WAITING_HUMAN` for another human
review. A failed rework run moves to `BLOCKED`; the existing `retry` command
can resume it on the same reviewed checkout. No new PR is opened, and a failed
or stale PR check prevents a push.

Two deliberate choices:

- **`VALIDATING → READY`** is the retry edge: a failed gate sends the task back
  for another run in the same checkout and branch instead of opening a bad PR.
  The next one-shot pass gives the agent bounded gate names as QA feedback;
  validation runs again after that agent completes. This preserves WIP=1.
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

Before a persisted `DISCOVERED` or unstarted `READY` task advances toward
dispatch, the runtime asks its `IssueSource` to recheck that structured source
against current eligibility rules. A readable closed, unlabelled, or pull request
payload moves the task to `CANCELLED` with a durable transition record; it creates
no run or workspace. Any source read failure, including HTTP 404, stops the
invocation without changing the task or its transition history and without
dispatching it: a 404 can also mean the token lacks access to a private repository.
Tasks with recovery history, active runs, and human-review states retain their
existing recovery behavior.

Intake persists a new eligible issue as `DISCOVERED` without a transition row.
When the runtime selects it, it rechecks eligibility, records `DISCOVERED → READY`
through `TaskLifecycleService`, then dispatch claims `READY → CLAIMED`. A CODE
task in `WAITING_HUMAN` continues to hold later CODE tasks, while an OPERATIONAL
scratch task can be selected and completed during that human review. The runtime
still processes only one task per invocation.

### Transition persistence

`TaskLifecycleService.transition()` retrieves the task, validates the requested
transition with the pure `TaskStateMachine`, then applies the status change and
inserts the history row **in one transaction**. If either half fails, neither is
committed: the status stays put and no history row appears. An invalid
transition raises before any write occurs.

## Publication and the human gate (Phase 5)

Phase 5 is the first time the factory writes to a remote: it commits the
validated run's workspace, pushes its isolated branch, opens a pull request and
stops at `WAITING_HUMAN`. Merging is a human action and is **not** implemented
anywhere.

```
VALIDATING
   ↓  guard:  task == VALIDATING, run belongs to task, run is latest,
   ↓          run.status == SUCCEEDED, validation == READY_FOR_NEXT_PHASE,
   ↓          workspace exists, workspace branch is not the default branch
   ↓  WorkspacePublisher.publish   (commit + push the isolated branch)
   ↓  PullRequestSink.find_open / open   (recover or create the PR)
   ↓  PullRequestRepository.save   (durable, one PR per run)
   ↓  VALIDATING → PR_OPEN → WAITING_HUMAN
STOP  (never merge)
```

### Publication orchestration

`PublicationService` (orchestration) takes a task id and run id and depends only
on domain ports — `TaskRepository`, `RunRepository`, `PullRequestRepository`,
`WorkspacePublisher` and `PullRequestSink`. It never imports GitHub, OpenHands,
SQLite, git or `subprocess`. The concrete work lives in integrations:

- `integrations/workspace/git_publish.py` — `GitWorkspacePublisher`;
- `integrations/github/pull_requests.py` — `GitHubPullRequestSink`;
- `integrations/github/write_client.py` — `GitHubWriteClient`.

### Publication guard

Publication is refused before any write unless every condition holds. A task that
is not `VALIDATING`, a run that does not belong to the task, a **superseded**
run, a run that did not `SUCCEED` or whose validation is not
`READY_FOR_NEXT_PHASE`, a missing workspace, or a workspace branch that is a
default branch all raise `TaskNotPublishableError` with no commit, push or PR.
Combined with the validation outcome, this means **a red or incomplete
validation can never be published**; Phase 5 consumes the durable Phase 4 gate
result rather than re-running gates.

The latest-run guard reuses the Phase 4 durable ordering: only the newest run for
a task may drive publication, so a stale attempt cannot publish after a retry.

### Commit

`GitWorkspacePublisher` runs git with argv only, `shell=False`, a bounded timeout
and a minimal environment. Before committing it verifies, inside the run's
workspace only:

- the path exists and is a Git worktree;
- the checked-out branch is **exactly** `Workspace.branch`;
- that branch is not `main`/`master`/the configured default;
- the workspace belongs to the task's target repository.

The commit is never made on `main`, never in the source checkout. The message is
deterministic and factory-controlled — `factory: implement task <task-id>` — and
task/agent text is never copied into it. A branch with no publishable diff and no
previous factory commit is **refused** rather than committed empty.

### Secure push

Only `Workspace.branch` is pushed, to the same branch name on the remote. The
destination ref is validated explicitly; `main`, `master`, `HEAD` and any
`+`-prefixed or option-like ref are refused, and no force option is ever used, so
history is never rewritten. The push runs with git hooks disabled
(`core.hooksPath=/dev/null`) and credential helpers cleared.

The push **source** is the immutable commit SHA that was just verified against
`AgentRun.validated_revision` — never the mutable `refs/heads/<branch>`. A local
branch can be moved by another process between the tree check and `git push`
(a TOCTOU window), so sourcing the branch ref could send a commit that never
passed validation. The refspec is `<commit_sha>:refs/heads/<workspace.branch>`:
the source is pinned to the verified commit, the destination is exactly the
workspace branch, and there is no leading `+`, so this remains a normal
non-force push.

The write credential is a **separate** token (`GITHUB_WRITE_TOKEN`), never the
read-only intake token. HTTPS authentication uses a temporary `GIT_ASKPASS`
helper that contains no credential and reads it from a process environment
variable; the helper is removed after the single push. The write token is never
placed in a URL, argv, `.git/config`, a log or an exception, and it is never
handed to quality-gate subprocesses.
`GITHUB_WRITE_USERNAME` supplies the non-secret HTTPS username to that helper;
it defaults to `x-access-token` when unset. The username comes from factory
configuration, never from task or repository text.

The push destination is pinned to what Git will **actually** push to. The
publisher asks Git for its own resolution — `git remote get-url --push --all` —
so a `remote.<name>.pushurl` or a `url.*.insteadOf`/`url.*.pushInsteadOf` rewrite
cannot hide the real destination. A network publication must resolve to exactly
one target; multiple network destinations are refused. The target is parsed with
URL parsing — never substring matching — and accepted only when its host equals
`FACTORY_GITHUB_GIT_HOST` (default `github.com`) and its path is exactly
`/<owner>/<repo>` or `/<owner>/<repo>.git` for the task's `target_repository`. A
plaintext `http://`, `ssh://`, `git://` or scp-style remote, another host,
another owner or repository, a port, userinfo, a query or a fragment is
**refused** with `UnsafeRemoteError` before the credential is exposed. HTTPS
alone is not sufficient, and the URL is never echoed. The authenticated push is
issued by remote name, which Git resolves with the same algorithm that was
validated. For the same reason the GitHub write client accepts only an `https://`
API base URL with no userinfo, refusing anything else with
`InsecureWriteTargetError` before the transport is invoked. Local filesystem
remotes are unaffected and are pushed without a credential.

### Pull request identity

A PR is adopted as the factory's publication only when its provider identity
matches the intended one exactly: the target repository, the **head repository**
(GitHub `head.repo.full_name`, so a fork or another owner's repository is never
adopted), the head branch (`Workspace.branch`), the configured base branch, and
`state == open`. A PR with the right head branch but a different base — e.g.
`factory/task/ws -> release` where the factory expects `main` — is not a match;
the lookup keeps searching and may still find a later, correct PR. Missing or
malformed identity fields are treated as a non-match, never as success.

The same identity is enforced on recovery from durable storage:
`PublicationService` re-checks a PR returned by `get_for_run`/`find_by_branch`
(repository, head branch, base branch, and run/task ownership) before it can
short-circuit publication, and refuses a mismatch with `PullRequestIdentityError`
instead of reconciling the task to `WAITING_HUMAN`. A mismatched stored row is
never overwritten, and a failed-create fallback that finds only a wrong-base or
fork PR raises a sanitized `PublicationError`.

The same bar applies to a **successful create**. The `POST /pulls` response is
untrusted provider text, so it is mapped through the same structural matcher as a
lookup: it is accepted only when it confirms `state == open`, the head
repository/ref and the base repository/ref, and a positive integer number.
Factory identity is never synthesized over a response that does not match. A
wrong, closed or malformed success payload is instead resolved through an exact
lookup (recovering the real open PR if one exists); if none exists, publication
fails with a sanitized `PublicationError` and the task stays `VALIDATING`.
`PublicationService` re-validates the created PR's repository/head/base as
defense in depth before persisting it.

### Pull request persistence

`SqlitePullRequestRepository` stores one PR per run. The `pull_requests` table
carries `UNIQUE(run_id)` and `UNIQUE(repository_slug, head_branch)`, so a second,
different PR for the same run or branch is refused (`DuplicatePullRequestError`)
rather than silently changing identity. `save` is a plain insert, never an
upsert. The `opened_at` and `merged` fields round-trip; `merged` is bookkeeping
only. Initialization is idempotent `CREATE TABLE`/`CREATE INDEX`: an existing
database gains the table without losing data.

### Idempotency and crash recovery

Every step is independently retry-safe, and a persisted PR short-circuits the
flow, so a retry after a crash window:

| Window | Crash point | Retry behaviour |
|---|---|---|
| A | commit done, before push | reuse the existing commit and push it |
| B | push done, before PR | reuse the pushed branch, open the PR |
| C | provider PR created, before local persistence | find the open PR by exact identity (repository, head repository, head branch, base branch), persist it, no second PR |
| D | PR persisted, before `VALIDATING → PR_OPEN` | reconcile the lifecycle |
| E | task `PR_OPEN`, before `WAITING_HUMAN` | reconcile the lifecycle |
| F | task already `WAITING_HUMAN` | no-op returning the same PR |

Reconciliation applies only the legal publication edges, so repeated publication
adds no commit, no push, no PR row and no duplicate transition history. A retry
reuses the same commit, branch and PR, and leaves the task at `WAITING_HUMAN`.

### The stop line

`WAITING_HUMAN` is the hard stop. The factory may commit, push a branch, open a
PR, persist it and move a task to `WAITING_HUMAN`; it may **never** merge a PR,
enable auto-merge, push to main, or deploy after a PR. There is deliberately no
merge operation in `PullRequestSink`, `GitHubWriteClient` or the domain — an
automatic merge is not merely discouraged, it is unreachable.

## Automatic worker (Phase 6)

`factory watch` is a thin loop over the one-shot runtime, not a second pipeline:

```
python -m factory watch
      ↓
FactoryWatcher.run()                 (orchestration)
      ↓  per iteration (WIP = 1)
FactoryRuntime.run_once()            (reused, unchanged)
      ↓  NO_ELIGIBLE_TASK
sleep(FACTORY_WATCH_IDLE_INTERVAL)   (injected; default 60s)
      ↓  other non-terminal outcome
next iteration
      ↓  WAITING_HUMAN
stop after current iteration → human review
      ↓  SIGINT / SIGTERM
stop observed between iterations → clean exit
```

Design intent:

- **Reuse, not re-implementation.** The watcher holds a ``FactoryRuntime`` and
  calls it. It contains no intake, dispatch, tracking or publication logic, so
  the phase boundaries and safety guards are exactly those of ``factory run``.
- **WIP = 1 for active execution.** One runtime invocation runs at a time, and
  the runtime itself completes at most one task. A CODE task at
  ``WAITING_HUMAN`` holds later CODE tasks until human review, but a separate
  OPERATIONAL scratch task may run while that review is pending.
- **Cooperative stop.** The CLI installs ``SIGINT``/``SIGTERM`` handlers that only
  set a flag; the loop checks it between iterations, so a signal cannot interrupt
  an in-flight task or corrupt persisted state. Previous handlers are restored on
  exit.
- **Injected seams.** ``sleep`` and ``should_stop`` are constructor arguments, so
  the loop is deterministic and offline under test — no wall-clock or signal
  dependence in the tests.

The worker never merges, deploys or mutates an Issue. Its terminal state is
``WAITING_HUMAN``, same as the one-shot runtime.

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
dispatched.** Dispatch requires `READY` and checks for an active run before
claiming or creating a workspace. The original run is preserved.
The guards are:

1. `DispatchService` requires `READY` and no active run before claiming.
2. `agent_runs` carries a partial unique index, `uq_agent_runs_active_task`,
   allowing at most one non-terminal (`PENDING`, `RUNNING`) run per task.

Terminal runs fall outside the partial index. If `adapter.dispatch` raises,
dispatch persists the workspace and a terminal `FAILED` run, then records
`CLAIMED → BLOCKED` through `TaskLifecycleService` before raising a sanitized
`AgentDispatchError`.

`python -m factory retry --task-id <uuid>` explicitly records `BLOCKED → READY`
for that task only, provided it has no active run. For legacy `CLAIMED` tasks,
retry additionally requires historical runs with the most recent run `FAILED`,
then records `CLAIMED → BLOCKED → READY`. The first transition requires the
state to remain exactly `CLAIMED`; both transitions use lifecycle compare-and-swap
and persist history. A missing history, a latest non-failed run, any active run,
other states and repeated requests are refused clearly. Concurrent retries
cannot duplicate recovery transitions.
Retry performs no intake or dispatch and never alters historical runs or
workspaces. An ordinary retry creates a new workspace, branch and run identity;
a QA rework retry keeps its reviewed checkout and branch. There is no automatic
retry loop.

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

## Codex adapter (Issue #28)

`FACTORY_AGENT_ENGINE=codex|openhands` selects exactly one adapter at runtime
construction. The temporary default is `codex`; explicit `openhands` retains
the local Agent Server path. The runtime checks a persisted run's `AgentKind`
before collection, so changing the setting cannot move a run to another engine.

`CodexAdapter` executes `codex exec` with `--sandbox workspace-write` and
`--cd` set to the Factory-provisioned checkout. It sends the bounded task
instruction on stdin, uses the service user's existing ChatGPT login through
`HOME`/`CODEX_HOME`, and does not pass an API key. Dispatch returns a running
record promptly; a worker writes a sanitized result beside the checkout for
collection after a factory restart. A timeout kills the CLI process group.
Nonzero exits, missing or malformed final messages, launch errors and timeouts
produce `RunStatus.FAILED`; a zero exit with a valid final message produces
`SUCCEEDED`. The adapter does not perform Factory's commit, push, PR or
lifecycle steps.

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
- preparation is **retry-safe**: an existing, matching workspace keeps its identity
  and has its permissions repaired; a pre-existing directory or a worktree on the wrong branch is **refused**, never
  silently reused;
- failures are normalized to a sanitized `WorkspaceProvisioningError`; git's
  stderr — which can echo a remote URL with an embedded token — is discarded
  rather than chained into the error;
- the source checkout location is **injected** (`FACTORY_SOURCE_CHECKOUT`), never
  a hard-coded machine path.

Permission normalization skips chmod when the current mode already matches;
compliant foreign-owned files therefore require no ownership privileges.
Ignored directories are owner-only (`0700`). See the read-only
[OpenHands audit](openhands-shared-workspace-audit.md) for why umask alone cannot
fix the editor's new-file `0600` atomic writes, and
[the shared-workspace runtime runbook](openhands-shared-workspace-runtime.md) for
the owner-side hook that repairs them.

Workspace permissions are independent of the parent process umask: directories
are `2770` (setgid, owner/group rwx, no others), regular checkout files are `0660`,
and owner-executable files are `0770`. The configured workspace root must already
allow shared-group traversal and carry the shared group; a newly created root is
set to `2770`. No group ownership or infrastructure is changed. Symlinks are not
followed, hardlinks/special files are refused, and external Git metadata is never
traversed. Ignored files retain owner-only access (`0600`/`0700`) because they can
contain local secrets. No process-global umask change is needed.

The policy itself lives in **one** module,
`integrations/workspace/shared_policy.py`. Both the Factory-side provisioner
repair and the OpenHands-side owner normalizer call it, so the two cannot drift
into incompatible rules. The OpenHands side is a synchronous command hook
(`PostToolUse` on `file_editor`, plus a blocking `Stop`) attached to each
conversation's `hook_config`; it normalizes the isolated workspace as its owner
(UID 10001) and exits `2` when it cannot enforce the policy, so an unsafe or
unrepairable workspace cannot be reported as a successful run. The Factory-side
post-agent repair remains as defense in depth.

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

A latest `SUCCEEDED` run with persisted green required gates but **no** bound
revision can recover while its task is `VALIDATING`, provided neither durable PR
storage (run or branch) nor the provider has a matching PR. Reconciliation repairs
the same workspace, fingerprints it, executes **all configured gates again**,
and fingerprints it again. Only identical fingerprints and green required gates
bind a revision. No adapter call, new run or new workspace is involved. Failed
required gates are not retried automatically; once bound, refresh is idempotent.
A failed revalidation keeps the task `VALIDATING` and blocks publication.

`CLAIMED → RUNNING` happens only *after* the run is durable, so a task is never
`RUNNING` without a run behind it.

### The validated workspace revision

A green gate result is necessary but not sufficient to publish. The run must also
carry the identity of the exact workspace revision that passed the gates, and
publication must re-verify it, because a completed coding agent (or a concurrent
writer) can change the workspace — or its Git configuration — after validation.

`RunTrackingService` first loads the task and calls `WorkspaceProvisioner.repair`.
This operation refuses missing checkouts and confirms the existing branch; it
reuses preparation's permission policy without creating a worktree or changing
contents. Tracking checks that the returned workspace identity is unchanged.
It then uses `WorkspaceRevisionInspector` immediately **before** and **after**
the gates and binds the identity only when the two match:

- required gate failed → no revision bound;
- workspace changed while the gates ran → a required, factory-controlled
  `workspace_integrity` gate is recorded as failed (detail
  `workspace changed during validation`) and no revision is bound. The configured
  gates are **never silently re-run**;
- repair or inspection fails (including a missing provisioner/inspector) → a
  required failed `workspace_integrity` gate with a constant sanitized detail,
  `GATES_FAILED`, and no bound revision. Raw errors and paths are discarded;
  publication is not attempted by the runtime.

The concrete `GitWorkspaceRevisionInspector` computes a **Git tree object id**
over a private temporary index (`read-tree HEAD` → `add -A` → `write-tree`). That
is exactly the publishable state a later `git add -A && git commit` would produce
— including deletions, modes and symlinks, excluding ignored files — without
touching the real index or creating a commit. The identity is durable: it is
persisted on `AgentRun.validated_revision` and survives a restart.

Publication verifies it twice:

- `PublicationService` refuses a new publication when the run has no bound
  revision (`ValidatedRevisionMissingError`). This is deliberately checked only on
  the *create* path, so reconciling an already-published run (crash windows C–F)
  is never blocked;
- `GitWorkspacePublisher` re-inspects the workspace and refuses on a mismatch
  (`ValidatedRevisionMismatchError`) **before** any commit or credential exposure,
  and again checks the resulting commit's tree before the push.

The fingerprint is opaque to `domain` and `orchestration`; materialising it needs
the filesystem and a VCS, so the port lives in `domain` and the Git implementation
in `integrations/workspace`.

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
domain nor orchestration hard-codes a command. This profile belongs to the
configured target repository. `FACTORY_TASK_QUALITY_GATES` may add checks keyed
by source issue reference. The example profile configures pytest, Ruff check,
Ruff format check and mypy as required checks. Gate statuses and allowlisted
execution details are persisted on each run and shown in Control Tower.

## Execution trace

`audit_events` is an append-only projection of committed task, transition, run,
and pull-request rows. SQLite triggers insert it in the same transaction as each
fact. The task id is the stable `correlation_id`; `event_seq` orders facts for a
task, while `aggregate_version` orders facts for an individual task, run, or PR.
`causation_id` refers to the persisted transition, run, or PR involved. A unique
`event_key` and existing lifecycle compare-and-swap prevent duplicate semantic
events on watcher retries. The state machine and `transitions` remain the source
of truth; `status_events` remains the delivery outbox for status consumers.

`GET /factory/trace?task_id=<id>` serves a read-only JSON trace on the loopback
Control Tower server. Provider run identifiers are hashed in this projection.
No task body, run summary, gate detail, PR title/body, workspace path, or provider
payload is stored as audit evidence. Initialization adds the table and triggers
to existing databases and projects their saved task transition history once.

## Planned evolution

| Phase | Addition |
|---|---|
| 2A | ✅ GitHub Issue intake + SQLite task/transition persistence |
| 2B | ✅ Run lifecycle persistence (`AgentRun`, `Workspace`) + `DispatchService` |
| 3 | ✅ OpenHands `AgentAdapter` (dispatch, collect, cancel) |
| 4 | ✅ Isolated Git worktree workspace per run; quality-gate validation in `VALIDATING` |
| 5 | ✅ Commit/push the isolated branch, open a PR, persist it, hand off at `WAITING_HUMAN` |
| 6 | API / dashboard over the orchestrator; Codex CLI adapter added in Issue #28 |
