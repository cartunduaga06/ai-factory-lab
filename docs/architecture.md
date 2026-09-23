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
│  FactoryTask · AgentRun · Repository · Workspace ·            │
│  PullRequest · QualityGate · AgentAdapter (protocol)          │
│  pure data + invariants — no I/O                              │
└───────────────────────────▲───────────────────────────────────┘
                            │ depends on
┌───────────────────────────┴───────────────────────────────────┐
│                        orchestration                          │
│  TaskStateMachine · lifecycle transition table · dispatcher   │
│  depends on the AgentAdapter protocol, never a concrete engine│
└───────────────────────────▲───────────────────────────────────┘
                            │ depends on
┌───────────────────────────┴───────────────────────────────────┐
│                        integrations                           │
│  GitHub (IssueSource, PullRequestSink)                        │
│  OpenHands adapter · Codex adapter · AgentAdapterBase         │
└───────────────────────────▲───────────────────────────────────┘
                            │ depends on
┌───────────────────────────┴───────────────────────────────────┐
│                      infrastructure                           │
│  FactoryConfig (env) · logging · persistence (future)         │
└───────────────────────────────────────────────────────────────┘
```

Dependencies point inward. Only `infrastructure` and `integrations` touch the
outside world.

## Package layout

```
src/factory/
├── __init__.py              # public surface
├── __main__.py              # `python -m factory` config sanity check
├── domain/
│   ├── enums.py             # TaskStatus, RunStatus, QualityGateStatus, ...
│   └── models.py            # dataclasses + AgentAdapter protocol
├── orchestration/
│   ├── lifecycle.py         # transition table (single source of truth)
│   └── machine.py           # TaskStateMachine, InvalidTransitionError
├── integrations/
│   └── base.py              # IssueSource, PullRequestSink, AgentAdapterBase
└── infrastructure/
    ├── config.py            # FactoryConfig.from_env + redaction
    └── logging.py           # configure_logging
```

## Domain model

The seven concepts from the specification, and where they are defined:

| Concept | Definition | Notes |
|---|---|---|
| `FactoryTask` | `domain/models.py` | `external_ref` links back to a GitHub Issue; `status` is a `TaskStatus`. |
| `AgentRun` | `domain/models.py` | One attempt; carries `adapter: AgentKind`, its `Workspace` and `QualityGate[]`. |
| `Repository` | `domain/models.py` | `slug` + `RepositoryRole` (`CONTROL_PLANE` / `TARGET`). |
| `Workspace` | `domain/models.py` | Ephemeral, per-run, must have a branch. |
| `PullRequest` | `domain/models.py` | Records the proposal; merge is external and human. |
| `QualityGate` | `domain/models.py` | Named check with `QualityGateStatus`; required gates block. |
| `AgentAdapter` | `domain/models.py` | `runtime_checkable` `Protocol` — the engine seam. |

Design intent:

- **Engine independence.** Orchestration sees only `AgentAdapter.kind`,
  `dispatch`, `collect` and `cancel`. Adding Codex or any other engine is a new
  integration, not an orchestration change.
- **Issues as the source of work.** `FactoryTask.external_ref` is deliberately a
  string, not an integer, so PRs and other artifacts can be referenced later.
- **Immutable value types.** `Repository`, `Workspace`, `PullRequest` and
  `QualityGate` are frozen; `FactoryTask` and `AgentRun` are mutable because the
  lifecycle advances them.

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

No workflow engine is implemented. The machine validates transitions; scheduling
and persistence come later.

## Configuration model

`FactoryConfig.from_env()` reads environment variables only, groups them by
integration (`github`, `openhands`, `codex`, database, logging) and defaults
safely so the repository runs with nothing configured:

- Missing or empty values → `None`, never a real default credential.
- Unresolved `<placeholder>` values (from `.env.example`) are treated as unset,
  so a copied example file cannot masquerade as configuration.
- `FactoryConfig.redacted()` is the only supported way to log configuration.

See `.env.example` for the full reference.

## Planned evolution

| Phase | Addition |
|---|---|
| 2 | GitHub Issues → `FactoryTask` intake; persistence for tasks/runs/transitions |
| 3 | OpenHands `AgentAdapter`; workspace provisioning |
| 4 | Gate evaluation inside `VALIDATING` |
| 5 | PR creation, `WAITING_HUMAN` handoff; Codex adapter |
| 6 | API / dashboard over the orchestrator |
