# AI Factory Lab

AI Factory Lab is the **control plane** of a software-development factory. It
coordinates autonomous coding agents (OpenHands, Codex and future engines) that
work on *external* product repositories — starting with
[`cartunduaga06/finanza-ia`](https://github.com/cartunduaga06/finanza-ia).

The factory owns *workflow*. It does not own *product code*.

```
Human / Dashboard / API
        │
        ▼
AI Factory Orchestrator
        │
        ▼
Task / Issue Queue
        │
        ▼
Agent Dispatcher
        │
        ▼
OpenHands / Codex
        │
        ▼
isolated workspace / branch
        │
        ▼
implementation
        │
        ▼
tests / QA gates
        │
        ▼
Pull Request
        │
        ▼
human approval
        │
        ▼
merge
```

## 1. What AI Factory Lab is

A repository is a unit of work waiting to be automated. AI Factory Lab is the
system that decides *what* gets automated, *which agent* does it, *how* the work
is verified, and *when* a human must step in. Its responsibilities:

- **Intake** — turn GitHub Issues into structured, typed tasks.
- **Dispatch** — route a task to an agent engine through a stable adapter.
- **Isolate** — give every run its own throwaway workspace and branch.
- **Verify** — apply quality gates (lint, tests, type checks) before a PR is
  proposed.
- **Escalate** — open a Pull Request and hand the merge decision to a human.

GitHub Issues are the initial source of work.

## 1a. Issue intake pipeline (Phase 2A)

The first operational capability turns GitHub Issues into durable local tasks:

```
GitHub Issue
      ↓
GitHubIssueSource
      ↓
FactoryTask
      ↓
IssueIntakeService
      ↓
TaskRepository
      ↓
SQLite
```

In words: `GitHubIssueSource` reads **open** issues labelled `factory-ready` over
the GitHub REST API (read-only) and maps each into a `FactoryTask` carrying a
structured `TaskSource`. `IssueIntakeService` — which knows nothing about GitHub
or SQLite, only the `IssueSource` and `TaskRepository` contracts — persists the
tasks that are new and leaves existing ones untouched. `SqliteTaskRepository`
stores them locally so they survive a restart.

Intake is manual and idempotent:

```bash
python -m factory intake
```

```
Discovered: 3
Created: 2
Existing: 1
Errors: 0
```

Running it again over the same issues reports `Created: 0` and creates no
duplicates. See [section 4](#4-current-development-status) for what is and is not
implemented.

## 1b. Run lifecycle and dispatch (Phase 2B)

The second capability claims a ready task and records the run it produces:

```
FactoryTask (READY)
      ↓
DispatchService  ──► AgentAdapter (protocol)
      ↓                    ↓
CLAIMED              AgentRun + Workspace
      ↓                    ↓
TaskRepository       RunRepository
      ↓                    ↓
            SQLite
```

In words: `DispatchService` requires the task to be `READY`, claims it with an
atomic `READY → CLAIMED` compare-and-swap, builds an isolated `Workspace` for it,
calls the `AgentAdapter` protocol, and persists the resulting `AgentRun` together
with its workspace. The service reads only `AgentAdapter.kind`, so no engine is
named anywhere in orchestration. `SqliteRunRepository` stores workspaces and runs
so they survive a restart.

The factory still ships **no real engine** in Phase 2B. Phase 2B proves the seam
with a deterministic fake adapter in tests; OpenHands arrives in Phase 3.

Dispatch is safe to repeat. The idempotency rule is: **a task that already has an
active run — one whose `RunStatus` is not terminal — has already been dispatched.**
Such a task is left `RUNNING` (or `CLAIMED` if provisioning is still in progress),
so the `READY` requirement refuses the retry and the original run is kept. The
rule is enforced twice: by the `READY` check in `DispatchService`, and by a
partial unique index (`uq_agent_runs_active_task`) that allows at most one
non-terminal run per task. Terminal runs fall outside that index, so a genuine
retry after a finished run is still possible.

Two dispatchers racing for the same `READY` task resolve deterministically:
exactly one wins, the loser is refused in a controlled way, and exactly one
transition, one workspace and one run exist afterwards.

## 1c. Isolated workspaces and validation (Phase 4)

Phase 4 gives each dispatch attempt its own physical Git workspace and adds the
validation lifecycle:

```
FactoryTask (READY)
      ↓
DispatchService  ──► WorkspaceProvisioner ──► git worktree add
      ↓                                          ↓
CLAIMED → RUNNING          Workspace (branch factory/<task>/<workspace>)
      ↓
AgentAdapter.dispatch ──► AgentRun persisted
      ↓  (later)
RunTrackingService ──► AgentAdapter.collect
      ↓
SUCCEEDED → Task VALIDATING ──► QualityGateRunner in the workspace
                    ↓
          gates persisted on the AgentRun; task STAYS VALIDATING
```

One **unique branch and worktree per run attempt**: the branch and path derive
from the workspace's own generated id (`factory/<task-id>/<workspace-id>`), which
exists before the engine is involved. A retry of the same task therefore gets a
fresh workspace, never another run's tree. `GitWorktreeWorkspaceProvisioner`
creates the worktree, leaves the source checkout on its existing branch, and never
fetches, pushes or rewrites history.

`RunTrackingService` refreshes an active run through `AgentAdapter.collect()`
only, so it names no engine. `PENDING`/`RUNNING` leave the task `RUNNING`;
`SUCCEEDED` moves it `RUNNING → VALIDATING`; `FAILED` and `CANCELLED` use the
existing legal failure and cancellation edges.

In `VALIDATING`, configured gates run in the run's workspace through
`LocalQualityGateRunner` (argv only, `shell=False`, bounded timeout, no shell
interpolation, minimal environment). A required gate is green **only** when it
`PASSED`; `PENDING`, `FAILED` and `SKIPPED` do not count. When every required gate
passes the run reports `READY_FOR_NEXT_PHASE` — and the task **stays
`VALIDATING`**, because no pull request exists yet. Opening one is Phase 5 work.

Which gates exist is application configuration, never code:

```bash
FACTORY_SOURCE_CHECKOUT=~/projects/finanza-ia
FACTORY_QUALITY_GATES=[{"name":"tests","argv":["pytest"],"required":true}]
```

No secrets are required for this phase's tests or smoke run: they use disposable
local repositories.

## 1d. Publication and the human gate (Phase 5)

Phase 5 turns a green validation into a reviewable proposal and stops at a
mandatory human checkpoint:

```
Task VALIDATING · run SUCCEEDED · validation READY_FOR_NEXT_PHASE · isolated workspace
      ↓  guard
WorkspacePublisher.publish          commit + push the isolated branch
      ↓
PullRequestSink.find_open / open    recover or create the PR
      ↓
PullRequestRepository.save          durable, one PR per run
      ↓
VALIDATING → PR_OPEN → WAITING_HUMAN
STOP                                never merge
```

Publication is refused before any write unless the run is the task's **latest**,
`SUCCEEDED`, `READY_FOR_NEXT_PHASE`, with an isolated workspace on a non-default
branch. A red or incomplete gate result therefore can never become a commit, a
push or a PR — Phase 5 consumes the durable Phase 4 validation result rather than
re-running gates.

The guard is not just "the gates were green": the run must also carry the
identity of the exact workspace revision that passed them. Validation fingerprints
the workspace immediately before and after the gates and binds that identity on
`AgentRun.validated_revision` (a Git tree object id, computed over a private
temporary index so the real index is never touched and no commit is created). If
the workspace changed while the gates ran, a required `workspace_integrity` gate
fails and nothing is bound. `GitWorkspacePublisher` re-verifies the revision
before committing and again on the resulting commit's tree before pushing, so a
completed agent that edits the workspace after validation cannot publish the
altered state.

`GitWorkspacePublisher` commits only inside the run's workspace and pushes only
`Workspace.branch`, to the same branch name, with no force option ever used:
`main`/`master`/`HEAD` are refused and history is never rewritten. The commit
message is deterministic (`factory: implement task <task-id>`); task and agent
text are never copied into it. A branch with no publishable diff is refused
rather than committed empty. The commit's tree is re-checked against the validated
revision, and the push then sources that verified commit SHA directly
(`<commit_sha>:refs/heads/<branch>`) rather than the mutable local branch, so a
concurrent move of the branch cannot change what reaches the remote.

`GitHubPullRequestSink` recovers an open PR only when its provider identity
matches the intended publication exactly — same target repository, head
repository, head branch, base branch and `state == open` — and otherwise opens
one, with an explicit base branch (`FACTORY_TARGET_DEFAULT_BRANCH`, default
`main`). A PR with the right head branch but a different base, or a head from a
fork, is never adopted. The PR
body carries only safe factory metadata — source Issue reference, task/run ids,
validation outcome and gate names/statuses. GitHub's own response text is never
propagated: only the numeric HTTP status crosses the client boundary.

The write credential is a **separate** `GITHUB_WRITE_TOKEN`, never the read-only
intake token. HTTPS push uses a temporary `GIT_ASKPASS` helper that holds no
credential; a remote URL with embedded userinfo, or a plaintext `http://` remote,
is refused, and the GitHub write client accepts only an `https://` API base URL.
Git hooks are disabled for factory-controlled commit and push.

**The factory stops at `WAITING_HUMAN`.** It may commit, push a branch, open and
persist a PR, and hand off to a human. It may never merge, enable auto-merge, push
to `main` or deploy — there is deliberately no merge capability anywhere in the
domain, the sink contract or the GitHub client. Publication is idempotent and
restart-safe: a retry after a crash at any step reuses the same commit, branch and
PR and adds no duplicate row or transition.

## 2. Why it is separate from product repositories

`ai-factory-lab` and `finanza-ia` have different lifespans, secrets and risk
profiles.

| | ai-factory-lab | finanza-ia |
|---|---|---|
| Role | orchestration / control plane | product repository / execution target |
| Owns | workflow, adapters, policy | application code |
| Change cadence | evolves with tooling | evolves with the product |
| Credentials | factory + dispatch secrets | product secrets |

If the factory's code lived inside the product, an agent improving the factory
could accidentally deploy the product, and product releases would be coupled to
orchestration changes. Keeping them apart means the factory can be rebuilt,
audited or paused without touching what it produces. **Finanza IA application
code is never copied into this repository.**

## 3. High-level architecture

Four layers, each depending only inward:

```
domain        ← pure typed model, no I/O
orchestration ← lifecycle + dispatch; depends on the AgentAdapter protocol only
integrations  ← GitHub, OpenHands, Codex adapters
infrastructure← configuration, logging, persistence
```

The orchestration layer never imports a concrete engine, which is what lets
multiple agents coexist. Full diagram and rationale:
[`docs/architecture.md`](docs/architecture.md).

## 4. Current development status

**Phase 1 — repository baseline. Complete.**
**Phase 2A — GitHub issue intake + persistence. Complete.**
**Phase 2B — run lifecycle and dispatch. Complete.**
**Phase 3 — OpenHands adapter. Complete.**
**Phase 4 — isolated workspaces and quality gates. Complete.**
**Phase 5 — commit/push, Pull Request and the human gate. Implemented on this branch.**

Present today:

- Repository structure and architectural documentation.
- Typed domain model: `FactoryTask`, `TaskSource`, `TaskTransition`, `AgentRun`,
  `Repository`, `Workspace`, `PullRequest`, `QualityGate`, `AgentAdapter`.
- A declarative task lifecycle with a validating state machine.
- An environment-variable configuration model with credential redaction.
- A Python 3.11+ skeleton with pytest, Ruff and mypy baselines.
- **Read-only GitHub issue intake** (`GitHubIssueSource`) mapping `factory-ready`
  issues into `FactoryTask` objects with structured source identity.
- **SQLite persistence** (`SqliteTaskRepository`) for tasks and an auditable
  transition history, with a database-level uniqueness constraint on source
  identity.
- An **idempotent, provider-agnostic `IssueIntakeService`** and the manual
  `python -m factory intake` command.
- **A `DispatchService`** that claims a `READY` task through the atomic lifecycle
  and records its run through the `AgentAdapter` protocol only.
- **SQLite persistence for workspaces and agent runs**
  (`SqliteRunRepository`), with one-active-run-per-task enforced by a partial
  unique index.
- **An `OpenHandsAdapter`** (`factory/integrations/openhands/`) that drives a real
  OpenHands Agent Server conversation through the `AgentAdapter` protocol:
  `dispatch` creates the conversation for a task's workspace, `collect`
  normalizes its execution state back to `RunStatus`, and `cancel` interrupts it
  idempotently. Run identity is the OpenHands conversation id. No OpenHands SDK
  dependency: the adapter speaks HTTP through an injectable transport.
- **Per-run workspace isolation**: `new_workspace()` builds a workspace identity
  from its own generated id, and `GitWorktreeWorkspaceProvisioner` materialises it
  as a `git worktree` on `factory/<task-id>/<workspace-id>`, leaving the source
  checkout untouched.
- **The validation lifecycle**: `RunTrackingService` refreshes an active run
  through `AgentAdapter.collect()` and drives the task to `VALIDATING` on success,
  using the existing legal edges for failure and cancellation.
- **A local quality gate runner** (`LocalQualityGateRunner`): argv-only,
  `shell=False`, workspace-scoped, bounded, sanitized results.
- **Durable run updates**: `RunRepository.update_run()` refreshes a stored run's
  status, summary, timestamps, workspace and gates without creating a duplicate.
- **A `PublicationService`** that publishes a validated run: it commits and pushes
  the run's isolated branch through `WorkspacePublisher`, recovers or opens a PR
  through `PullRequestSink`, persists it through `PullRequestRepository`, and
  advances the task `VALIDATING → PR_OPEN → WAITING_HUMAN`. It depends on ports
  only and never merges.
- **A `GitWorkspacePublisher`**: argv-only, hook-isolated, credential-separated
  commit and non-force push of exactly the workspace's branch.
- **A `GitHubPullRequestSink`** and write-capable `GitHubWriteClient`: recover an
  open PR only on an exact provider identity match (repository, head repository,
  head branch, base branch) or open one, with only the numeric status escaping a
  failed request.
- **Durable PR persistence** (`SqlitePullRequestRepository`): one PR per run,
  enforced by a unique index and surviving a repository reopen.

Not present yet — deliberately deferred:

- **Codex and other engines.** OpenHands is the only concrete engine; the
  `AgentAdapter` seam still admits others.
- **Any merge capability.** The factory stops at `WAITING_HUMAN`; merge,
  auto-merge and deploy are human actions and are absent from the code.
- **A scheduler or daemon.** Intake is a single manual run; dispatch, tracking and
  publication are called programmatically.
- **A dashboard, API or FastAPI service.**
- **PostgreSQL.** Persistence is SQLite only; `DATABASE_URL` rejects other
  schemes.

These are Phase 6 and later work. See the [roadmap](#7-planned-roadmap).

## 5. Local development setup

Requires Python 3.11 or newer.

```bash
git clone https://github.com/cartunduaga06/ai-factory-lab.git
cd ai-factory-lab

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[dev]"

cp .env.example .env               # placeholders only; fill in locally, never commit
```

Run the checks:

```bash
pytest
ruff check .
ruff format --check .
mypy
```

Inspect the resolved configuration without exposing secrets:

```bash
python -m factory --show-config
```

`.env` is git-ignored. `.env.example` contains placeholders only and must never
hold a real credential.

## 6. Safety model

The factory is built so that the dangerous steps require a human.

Agents **may**: inspect assigned repositories, create isolated branches and
workspaces, edit files within their assigned task, run allowed tests, commit
changes, and open Pull Requests.

Agents **may not**: merge PRs, push directly to `main`, deploy production,
modify production secrets, change GitHub repository permissions, alter unrelated
repositories, or modify host infrastructure.

Human approval remains mandatory for merge and production deployment. This
policy is stated in full — with the reasoning behind each boundary — in
[`docs/security.md`](docs/security.md).

## 7. Planned roadmap

| Phase | Focus |
|---|---|
| 1 ✅ | Repository baseline: structure, domain model, state machine, config, tooling |
| 2A ✅ | GitHub Issue intake → `FactoryTask`; SQLite task + transition persistence |
| 2B ✅ | Run lifecycle persistence (`AgentRun`, `Workspace`); `DispatchService` over the `AgentAdapter` protocol |
| 3 ✅ | `OpenHandsAdapter`: real OpenHands Agent Server dispatch, collect and cancel |
| 4 ✅ | Isolated `git worktree` workspace per run; quality gates evaluated in `VALIDATING` |
| 5 ✅ | Commit/push the isolated branch, open and persist a PR, hand off at `WAITING_HUMAN` (never merge) |
| 6 | API / dashboard on top of the orchestrator |

Each phase is delivered through a Pull Request and is never merged by the agent
that produced it.

## Contributing

Repository conventions, layering rules and command reference live in
[`AGENTS.md`](AGENTS.md). Architecture detail is in
[`docs/architecture.md`](docs/architecture.md), security policy in
[`docs/security.md`](docs/security.md), and the development workflow in
[`docs/development.md`](docs/development.md).
