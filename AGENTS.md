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

## Layering rules

Dependencies point inward only. Violating this is the most likely way to break
the architecture.

```
infrastructure ─┐
integrations  ──┼──► orchestration ──► domain
                ┘
```

- `factory/domain` — pure typed model. **No I/O**: no network, DB, filesystem,
  `os.environ`, `subprocess` or git access.
- `factory/orchestration` — lifecycle and dispatch. May depend on `domain`.
  **Must not import a concrete agent engine** — only the `AgentAdapter` protocol
  — and must not import `subprocess`, `sqlite3` or git. `DispatchService`
  depends on the `WorkspaceProvisioner` port; `RunTrackingService` on
  `QualityGateRunner`.
- `factory/integrations` — GitHub, OpenHands, workspace and gate adapters.
  Translates external APIs into domain types. `integrations/openhands/` talks to
  the OpenHands Agent Server over HTTP through an injectable transport; the
  factory never imports the OpenHands SDK, and no OpenHands code belongs in
  `domain` or `orchestration`. `integrations/workspace/git.py` creates one Git
  worktree per run; `integrations/gates/local.py` runs argv-only, shell-free,
  bounded gate processes.
- `factory/infrastructure` — configuration, logging, persistence. May not import
  `orchestration`. Core-domain `ports` live in `domain/ports.py`:
  `IssueSource`, `TaskRepository`, `RunRepository`, `WorkspaceProvisioner` and
  `QualityGateRunner`. Concrete GitHub behaviour belongs in
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
```

`python -m factory intake` runs one manual GitHub intake pass (read-only) and
persists eligible issues. There is no daemon or scheduler.

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
| `PullRequest` | Agent-produced PR awaiting mandatory human approval. |
| `QualityGate` | A named, verifiable check (lint, tests, typecheck) with `required`/`is_green`. |
| `QualityGateSpec` | Declarative argv definition of a gate supplied by the application layer. |
| `QualityGateRunner` | Port that executes a `QualityGateSpec` in a `Workspace`. |
| `ValidationOutcome` | Deterministic result of validating a run (`PENDING`/`READY_FOR_NEXT_PHASE`/`GATES_FAILED`). |
| `AgentAdapter` | Engine-agnostic execution interface (OpenHands, Codex, ...). |
| `IssueIntakeService` | Idempotent intake: eligible issues → persisted tasks. |
| `TaskLifecycleService` | Validates a transition, then persists status + history atomically. |
| `DispatchService` | Claims a task, provisions a per-run workspace, starts the run. |
| `RunTrackingService` | Refreshes an active run, drives lifecycle, evaluates gates on success. |

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
- Configuration reference: `.env.example`
