# AGENTS.md — AI Factory Lab

Persistent repository context for AI agents working in this codebase. Read this
before making changes.

## What this repository is

`ai-factory-lab` is the **orchestration / control plane** for AI Factory Lab. It
coordinates autonomous coding agents (OpenHands, Codex) that operate on external
product repositories, starting with `cartunduaga06/finanza-ia`.

`ai-factory-lab` (control plane) and `finanza-ia` (product / execution target)
are separate repositories with separate concerns. **Never copy Finanza IA
application code into this repository.**

## Hard rules

1. Never work directly on `main`. Use a feature branch.
2. Never merge a Pull Request. Merge is a human decision.
3. Never push to `main` of any repository, including product repositories.
4. Never deploy anything or modify host / Docker / OpenHands infrastructure.
5. Never modify GitHub repository settings or permissions.
6. Never commit a secret. `.env` is git-ignored; `.env.example` holds placeholders only.
7. Never modify `finanza-ia`.
8. Do not perform destructive git operations (force-push, history rewrite, branch deletion).

## Code review policy

When reviewing a Pull Request, act as a defensive reviewer of the AI Factory
control plane. Prioritize correctness and architectural safety over style.

Review in this order:

1. Correctness and regressions
   - Identify behavior that can break an existing Factory lifecycle.
   - Verify failure paths, retries and partial failures.
   - Flag state transitions that bypass the lifecycle state machine.

2. Worker and workspace isolation
   - A run must operate only on its assigned workspace and branch.
   - Changes from concurrent workers must not leak across workspaces.
   - Look for race conditions, shared mutable state and unsafe concurrency.

3. Architecture boundaries
   - domain must remain pure and I/O-free.
   - orchestration must depend on ports, not concrete integrations.
   - concrete GitHub, Git, OpenHands, persistence and gate logic stays outside
     domain/orchestration.
   - Flag duplicated policies or logic that already has a canonical owner.

4. Git and publication safety
   - Never allow direct writes to main.
   - Never introduce automatic merge behavior.
   - Never introduce force-push, history rewriting or destructive Git actions.
   - Publication must affect only the isolated branch belonging to the run.

5. Security
   - Flag secret exposure, unsafe shell execution, command injection,
     unbounded subprocesses and unsafe filesystem access.
   - External input from GitHub, agents or product repositories must be treated
     as untrusted.

6. Persistence and lifecycle integrity
   - Verify persisted state matches the actual runtime state.
   - State changes must remain auditable and deterministic.
   - Check idempotency where retries or repeated events are possible.

7. Tests and quality gates
   - New behavior must have tests for meaningful success and failure paths.
   - Concurrency changes require isolation or race-condition tests.
   - Do not consider a change ready when required tests, lint or type checks fail.

Review comments must identify a concrete defect or meaningful risk and explain
its impact. Avoid blocking a Pull Request for purely stylistic preferences
already covered by Ruff or formatting tools.

### Automatic code review contract

When acting as a Pull Request reviewer, operate read-only.

- Do not edit files, commit, push, merge, deploy, modify Issues, change repository
  settings or mutate Factory state.
- Review the PR diff together with surrounding code, affected callers, tests,
  persistence paths and the relevant base-branch behavior.
- Passing CI is necessary but not sufficient. Green pytest, Ruff and Mypy results
  do not prove architectural correctness, concurrency safety or recovery safety.
- Report only concrete defects or meaningful risks. Each finding should identify
  the affected file/line, the failure mechanism, its impact and the expected
  correction.
- Prefer a small number of high-confidence findings over speculative comments.
- Do not report formatting or style issues already enforced by Ruff or formatting
  tools.

### Cross-cutting compatibility

When a PR changes a Protocol, port, public method, enum, persisted model, schema,
configuration field, CLI contract or state transition, inspect all implementations,
test doubles, callers and persistence paths for compatibility.

A locally correct change that breaks another adapter, fake, migration path or
consumer is a defect. In particular, engine-specific capabilities must not be
added to a general protocol unless every supported engine is intentionally
required to implement them. Prefer optional capabilities or narrower protocols
when behavior is engine-specific.

### Persistence and migration safety

For SQLite or schema changes, review both:

- creation of a fresh database; and
- migration of an existing production database.

Check atomicity, idempotency, concurrent access, transition history and fail-closed
behavior. Do not accept a schema change that works only for a new database.

### Worker liveness and recovery invariants

- Supervisor heartbeat and agent/worker liveness are separate evidence.
- A persisted RUNNING task or refreshed supervisor heartbeat alone never proves
  that an agent process is alive.
- Never recover a demonstrably live worker.
- Orphan recovery must validate the exact latest run, workspace and branch
  identity, project routing/source eligibility, absence of a PR, absence of
  trusted terminal result evidence and workspace cleanliness.
- Missing or ambiguous recovery evidence must fail closed.
- Recovery requires explicit operator authorization.
- Recovery must not merge, deploy or dispatch a replacement run implicitly.
- Run/task recovery transitions must remain atomic and auditable.
- Review restart and supervisor-interruption behavior for changes involving
  workers, subprocesses or durable RUNNING state.

Pay particular attention to changes involving:
- task claiming;
- parallel workers;
- workspace provisioning;
- lifecycle transitions;
- persistence;
- Git publication;
- GitHub PR creation;
- quality gates;
- agent adapters and optional capabilities;
- protocols, ports and public contracts;
- database schemas and migrations;
- worker/supervisor liveness;
- recovery after worker failure or supervisor restart.

## Layering rules

Dependencies point inward only. Violating this is the most likely way to break
the architecture. `tests/test_layering.py` is the executable source of truth for
these import boundaries.

```
                  composition root
                 /       |        \
        orchestration  integrations  infrastructure
              |
              v
            domain
```

Core import rules:

- `domain` imports no outer Factory layer.
- `orchestration` imports neither `integrations` nor `infrastructure`.
- `infrastructure` does not import `orchestration`.
- Concrete engines, Git, GitHub, subprocess execution and persistence stay outside
  `domain` and `orchestration` unless represented through domain ports.

- `factory/domain` — pure typed model. **No I/O**: no network, DB, filesystem,
  `os.environ`, `subprocess` or git access.
- `factory/orchestration` — lifecycle and dispatch. May depend on `domain`.
  **Must not import a concrete agent engine** — only the `AgentAdapter` protocol
  — and must not import `subprocess`, `sqlite3` or git. `DispatchService`
  depends on the `WorkspaceProvisioner` port; `RunTrackingService` on
  `QualityGateRunner`; `PublicationService` on the `WorkspacePublisher`,
  `PullRequestSink` and `PullRequestRepository` ports.
- `factory/integrations` — GitHub, OpenHands, workspace and gate adapters.
  Translates external APIs into domain types. `integrations/openhands/` talks to
  the OpenHands Agent Server over HTTP through an injectable transport; the
  factory never imports the OpenHands SDK, and no OpenHands code belongs in
  `domain` or `orchestration`. `integrations/workspace/git.py` creates one Git
  worktree per run; `integrations/workspace/git_publish.py` commits and pushes
  exactly the isolated branch; `integrations/gates/local.py` runs argv-only,
  shell-free, bounded gate processes. `integrations/github/pull_requests.py` and
  `write_client.py` are the only Phase 5 write path, and expose no merge.
  `integrations/workspace/shared_policy.py`
  is the single cross-UID shared-workspace permission policy: the Factory-side
  repair in `git.py` and the OpenHands owner-side hook executable call it, and it
  must not move into `domain`/`orchestration`.
- `factory/infrastructure` — configuration, logging, persistence. May not import
  `orchestration`. Core-domain `ports` live in `domain/ports.py`:
  `IssueSource`, `TaskRepository`, `RunRepository`, `WorkspaceProvisioner`,
  `QualityGateRunner`, `WorkspacePublisher`, `PullRequestSink` and
  `PullRequestRepository`. Concrete GitHub behaviour belongs in
  `integrations/github`; concrete SQLite behaviour belongs in
  `infrastructure/persistence`.

## Commands

```bash
pip install -e ".[dev]"     # install with dev tooling
pytest                      # run tests
ruff check .                # lint
ruff format --check .       # formatting check
ruff format .               # apply formatting
mypy                        # strict type checking
python -m factory --show-config   # redacted config dump
python -m factory pool      # continuous bounded worker pool
```

`python -m factory intake` runs one manual GitHub intake pass (read-only) and
persists eligible issues. `python -m factory run` retains serial capacity one:
it runs at most one resumable task through the existing phases and stops at
`WAITING_HUMAN` for code work. `python -m factory pool` is the continuous
bounded worker supervisor. It schedules independent task IDs into isolated
worker sessions up to `FACTORY_MAX_CONCURRENCY`; the current MVP supports a
maximum concurrency of 2. Each session constructs its own `FactoryRuntime`
while SQLite task, run, workspace and PR records remain authoritative.
`python -m factory watch` is a compatibility alias for the same continuous
pool supervisor. The pool waits `FACTORY_WATCH_IDLE_INTERVAL` seconds between
idle passes and handles `SIGINT`/`SIGTERM` cooperatively: it stops new
scheduling and allows active bounded invocations to finish. No runtime mode
merges or deploys, and independent worker failure must not stop a healthy peer.

All four checks must pass before a change is proposed. Python 3.11+.

## Conventions

- Python 3.11+, `from __future__ import annotations`, type hints everywhere.
- `dataclasses(slots=True)` in the domain; `frozen=True` for value-like types.
- Prefer `Enum` (str-based) over string literals for statuses.
- Small functions, explicit names, minimal dependencies.
- Comments explain *why*, not *what*. Docstrings state the contract and the
  boundary, not the implementation narrative.
- Tests target real logic — no mocks of the code under test. Tests must not read
  the real environment or network: pass explicit inputs. Persistence tests use a
  temporary SQLite file; the GitHub adapter is tested with an injected in-memory
  transport. Workspace and gate tests each build a disposable local Git repo or a
  temporary directory under `tmp_path` — never a real product repository.
  `tests/test_layering.py` enforces the dependency direction and that concrete
  workspace/gate implementations stay outside `domain`/`orchestration`.

## Key domain concepts

| Concept | Meaning |
|---|---|
| `FactoryTask` | A unit of work, carrying a structured `TaskSource` identity. |
| `TaskSource` | Frozen `(provider, repository_slug, issue_number)` identity of a task. |
| `TaskTransition` | Auditable record of one lifecycle status change. |
| `AgentRun` | One attempt by one engine to complete a task. |
| `RunRepository` | Persistence port for `Workspace` and `AgentRun`; `update_run` refreshes a stored run. |
| `Repository` | A repo the factory knows about, tagged `CONTROL_PLANE` or `TARGET`. |
| `Workspace` | An isolated per-run checkout on its own branch, keyed by its own id. |
| `WorkspaceProvisioner` | Port that materialises a `Workspace` into a physical checkout. |
| `PullRequest` | Agent-produced PR awaiting mandatory human approval; carries `run_id`. |
| `PublishedRevision` | Frozen `(commit_sha, branch)` identity of a published workspace revision. |
| `QualityGate` | A named, verifiable check (lint, tests, typecheck) with `required`/`is_green`. |
| `QualityGateSpec` | Declarative argv definition of a gate supplied by the application layer. |
| `QualityGateRunner` | Port that executes a `QualityGateSpec` in a `Workspace`. |
| `WorkspacePublisher` | Port that commits a workspace and pushes its isolated branch. |
| `PullRequestSink` | Port to find/open pull requests. Deliberately has no merge. |
| `PullRequestRepository` | Port that durably stores the PRs the factory opens. |
| `ValidationOutcome` | Deterministic result of validating a run (`PENDING`/`READY_FOR_NEXT_PHASE`/`GATES_FAILED`). |
| `AgentAdapter` | Engine-agnostic execution interface (OpenHands, Codex, ...). |
| `IssueIntakeService` | Idempotent intake: eligible issues → persisted tasks. |
| `TaskLifecycleService` | Validates a transition, then persists status + history atomically. |
| `DispatchService` | Claims a task, provisions a per-run workspace, starts the run. |
| `RunTrackingService` | Refreshes an active run, drives lifecycle, evaluates gates on success. |
| `PublicationService` | Publishes a validated run: commit, push, PR, persist, `WAITING_HUMAN`. Never merges. |

## Task lifecycle

```
DISCOVERED → READY → CLAIMED → RUNNING → VALIDATING → PR_OPEN → WAITING_HUMAN → DONE
```

Failure paths: `BLOCKED` (recoverable, back to `READY`), `FAILED`, `CANCELLED`.
The transition table in `factory/orchestration/lifecycle.py` is the single source
of truth; keep `docs/architecture.md` in sync with it.

## Where things live

- Architecture: `docs/architecture.md`
- Security policy and agent boundaries: `docs/security.md`
- Development workflow: `docs/development.md`
- Cross-UID shared-workspace audit (read-only): `docs/openhands-shared-workspace-audit.md`
- Shared-workspace runtime runbook: `docs/openhands-shared-workspace-runtime.md`
- Configuration reference: `.env.example`
