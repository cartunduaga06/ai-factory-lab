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

The factory still ships **no real engine**. Phase 2B proves the seam with a
deterministic fake adapter in tests; OpenHands arrives in Phase 3.

Dispatch is safe to repeat. The idempotency rule is: **a task that already has an
active run — one whose `RunStatus` is not terminal — has already been dispatched.**
Such a task is left in `CLAIMED`, so the `READY` requirement refuses the retry and
the original run is kept. The rule is enforced twice: by the `READY` check in
`DispatchService`, and by a partial unique index (`uq_agent_runs_active_task`) that
allows at most one non-terminal run per task. Terminal runs fall outside that
index, so a genuine retry after a finished run is still possible.

Two dispatchers racing for the same `READY` task resolve deterministically:
exactly one wins, the loser receives a `DispatchConflictError`, and exactly one
transition, one workspace and one run exist afterwards.

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
**Phase 3 — OpenHands adapter. Implemented on this branch.**

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

Not present yet — deliberately deferred:

- **Codex and other engines.** OpenHands is the only concrete engine; the
  `AgentAdapter` seam still admits others.
- **Branch creation in target repositories, commits, pushes and PR creation.**
  The factory records the branch a workspace intends to use but creates nothing
  on GitHub.
- **Quality-gate evaluation.** `QualityGate` exists in the model but is not yet
  evaluated.
- **A scheduler or daemon.** Intake is a single manual run; dispatch is called
  programmatically.
- **A dashboard, API or FastAPI service.**
- **PostgreSQL.** Persistence is SQLite only; `DATABASE_URL` rejects other
  schemes.

These are Phase 4 and later work. See the [roadmap](#7-planned-roadmap).

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
| 4 | Workspace provisioning; quality gates (lint/tests/type checks) evaluated as part of the run |
| 5 | PR creation and `WAITING_HUMAN` handoff; Codex adapter as a second engine |
| 6 | API / dashboard on top of the orchestrator |

Each phase is delivered through a Pull Request and is never merged by the agent
that produced it.

## Contributing

Repository conventions, layering rules and command reference live in
[`AGENTS.md`](AGENTS.md). Architecture detail is in
[`docs/architecture.md`](docs/architecture.md), security policy in
[`docs/security.md`](docs/security.md), and the development workflow in
[`docs/development.md`](docs/development.md).
