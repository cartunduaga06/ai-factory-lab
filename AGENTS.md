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

- `factory/domain` — pure typed model. **No I/O**: no network, DB, filesystem or
  `os.environ` access.
- `factory/orchestration` — lifecycle and dispatch. May depend on `domain`.
  **Must not import a concrete agent engine** — only the `AgentAdapter` protocol.
- `factory/integrations` — GitHub, OpenHands, Codex adapters. Translates external
  APIs into domain types.
- `factory/infrastructure` — configuration, logging, persistence. May not import
  `orchestration`.

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

All four checks must pass before a change is proposed. Python 3.11+.

## Conventions

- Python 3.11+, `from __future__ import annotations`, type hints everywhere.
- `dataclasses(slots=True)` in the domain; `frozen=True` for value-like types.
- Prefer `Enum` (str-based) over string literals for statuses.
- Small functions, explicit names, minimal dependencies.
- Comments explain *why*, not *what*. Docstrings state the contract and the
  boundary, not the implementation narrative.
- Tests target real logic — no mocks of the code under test. Tests must not read
  the real environment or network: pass explicit inputs.

## Key domain concepts

| Concept | Meaning |
|---|---|
| `FactoryTask` | A unit of work, sourced from a GitHub Issue (`external_ref`). |
| `AgentRun` | One attempt by one engine to complete a task. |
| `Repository` | A repo the factory knows about, tagged `CONTROL_PLANE` or `TARGET`. |
| `Workspace` | An isolated per-run checkout on its own branch. |
| `PullRequest` | Agent-produced PR awaiting mandatory human approval. |
| `QualityGate` | A named, verifiable check (lint, tests, typecheck). |
| `AgentAdapter` | Engine-agnostic execution interface (OpenHands, Codex, ...). |

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
