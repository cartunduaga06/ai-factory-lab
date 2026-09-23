# AI Factory Lab architecture

## Purpose

AI Factory Lab converts a Product Owner requirement into a reviewable, validated Draft pull request while preserving human control over merge and deployment.

## Current baseline — v0.2

```text
Product Owner
    |
GitHub Issue / requirement
    |
factory-orchestrator
    |
repository preflight
    |
minimum required specialists
    |
implementation
    |
durable remote checkpoint
    |
independent QA
    |
integration validation
    |
Draft PR
    |
READY_FOR_PO
    |
Product Owner decision
```

The baseline specialist roles are:

- `factory-tech-lead`
- `factory-backend-engineer`
- `factory-frontend-engineer`
- `factory-qa-engineer`

Only the orchestrator owns Git delivery.

## Separation of concerns

```text
AI Factory Lab                    Product repository
-----------------------------     -----------------------------
Factory orchestration             Application source
Reusable agent definitions        Domain/business rules
Factory safety policy             Project AGENTS.md
Reusable skills/patterns          Product tests
Templates                         Product infrastructure
Factory learning                  Product data and secrets
```

The Factory must inspect and respect the target repository rather than embedding product-specific assumptions.

## Migration strategy

The current Finanza IA agent definitions remain untouched while this independent repository establishes the canonical Factory baseline.

Phase 1: preserve and document the working v0.2 baseline here.
Phase 2: validate equivalent OpenHands discovery/execution from the independent Factory.
Phase 3: define a safe consumption/versioning mechanism for product repositories.
Phase 4: only after equivalence is proven, remove duplicated Factory definitions from product repositories.

No active product execution should be changed merely because this repository exists.

## Planned capability layers

Future capabilities may be organized as:

```text
AI Factory Lab
├── orchestration
├── agents
├── skills
├── patterns
├── templates
├── tools / MCP integrations
├── reusable assets
└── Factory QA / observability
```

These are planned layers, not permission to implement them all at once.
