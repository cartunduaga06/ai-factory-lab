# ADR-0001: Project Provisioning and Project Registry v2

- Status: Proposed — awaiting technical review
- Date: 2026-10-06
- Issue: [P0-02 — ADR: Project Provisioning y Project Registry v2](https://github.com/cartunduaga06/ai-factory-lab/issues/150)
- Scope: architecture and migration contract only; no runtime or infrastructure changes

## Context

The Control Tower needs to let an operator request a project without making the
dashboard the owner of project identity or provisioning state. Today, the
runtime parses operator-maintained `FACTORY_PROJECTS` JSON into an in-memory
`ProjectRegistry` of `ProjectProfile` values. Tasks already persist
`project_id` and `target_repository`; those values are immutable after task
creation. This routes configured projects but does not provide durable project
records, provisioning operations, approval history, or recovery from partial
external writes.

The design must preserve the existing factory lifecycle, isolate staging from
production, and keep repository and Trello writes behind least privilege and
human approval.

## Decision

1. **Project Registry v2 is the canonical source of project identity and
   configuration.** Each project has one stable, immutable `project_id`.
   Repository identity is an approved `provider + host + owner/name` tuple.
   Reassigning a project to another repository is a separately approved
   migration; never an in-place edit.
2. **The Control Tower is a request and inspection interface.** It submits a
   typed command to the application service and renders persisted state. It
   does not own state, infer identity from URLs, or call provider APIs directly.
3. **Trello is an optional projection.** A card may link to a canonical
   `project_id`, but card IDs, names, labels, list position, and edits never
   create or change project identity or registry state. Trello sync is
   idempotent and may be repaired from the registry.
4. **Provisioning is an explicit, durable saga.** Database transitions and
   audit events are atomic locally. GitHub/Trello side effects are reconciled
   by stable operation IDs and idempotency keys; distributed exactly-once
   execution is not claimed.
5. **Retries are explicit.** Every retry creates a new attempt under the same
   operation/project identity and preserves previous evidence. A `CANCELLED`
   operation is terminal; replacement requires a deliberate operator command.
   No watcher silently adopts or restarts it.
6. **Provisioning cannot merge, deploy, or delete a repository.** Repository
   creation, permission grants, and any action that cannot be safely reversed
   require persisted human approval before execution. Compensation disables
   Factory routing and reverses only approved ancillary resources; repository
   deletion is a separate human action.

## Domain contract

### ProjectSpec

The immutable, validated `ProjectSpec` is the canonical versioned definition.
It contains:

- `project_id`: stable slug, unique and never reused;
- `spec_version` and `revision`: schema version and monotonically increasing
  registry revision;
- `repository`: provider, host, owner, name, visibility, and expected default
  branch; visibility is explicit and defaults to private for any later create;
- `source_checkout`: server-local absolute checkout path, validated beneath
  configured source roots;
- `quality_gates`: named argv arrays with required flags; no shell strings;
- `context_profile`: enum (`repository` or `repository+ecc`);
- `ci_policy`: provider CI requirement and explicitly named required checks;
- `deployment_policy`: currently `human-only`; required deployment is a
  declaration, never permission to deploy;
- `trello_projection`: optional board/list/card references, not identity;
- `credential_refs`: secret-manager entry names/identifiers only, never values;
- `created_at`, `created_by`, `updated_at`, `updated_by`, and
  `provenance` for audit.

Secrets, tokens, arbitrary shell commands, provider permission scopes, and
unvalidated URLs are not accepted in a ProjectSpec. Repository owner and host
must match an operator allowlist. Quality-gate executables and arguments are
validated against the existing bounded argv-only runner policy. The registry
stores a canonical normalized representation and content hash; an edit creates
a new revision and audit fact.

A registry entry has a lifecycle status separate from provisioning operations:
`ACTIVE`, `DISABLED`, or `RETIRED`. Only `ACTIVE` projects may be routed to a
new task. Disabling blocks new dispatch but preserves historical records and
running-workspace ownership. Retirement is an explicit reviewed transition;
project IDs are never reused. Operation `READY` means provisioning finished,
not that every project lifecycle action is permanently enabled.

### Project operation

A `ProjectProvisioningOperation` has an immutable `operation_id`, stable
`project_id`, requested spec snapshot/hash, actor, idempotency key, current
state/version, approval evidence, attempt number, timestamps, and per-step
external resource references. Events are append-only and carry correlation and
causation IDs. Credentials and raw provider responses are excluded from events
and logs.

### State machine

Lifecycle happy path:

```text
REQUESTED -> VALIDATING -> VALIDATED -> AWAITING_APPROVAL -> PROVISIONING -> READY
```

- `REQUESTED`: command accepted and durably recorded; no external writes.
- `VALIDATING`: schema, uniqueness, path, policy, and read-only provider
  preflight checks run.
- `VALIDATED`: a frozen spec snapshot passed deterministic checks.
- `AWAITING_APPROVAL`: an authorized human sees the exact operation/hash and
  side effects; approval is bound to that hash and expires.
- `PROVISIONING`: only approved steps run, one bounded step at a time. Persist
  intent before the call and verified result after it.
- `READY`: every required step is verified; the project may receive future
  tasks. This does not imply deployment or an agent run.
- `FAILED`: no unresolved external side effect is known. Resume requires an
  explicit retry and a new attempt.
- `NEEDS_RECONCILIATION`: a provider call was ambiguous or partially applied.
  Dispatch is forbidden until an operator reconciles it.
- `CANCELLED`: terminal, with no automatic retry. It is allowed only when no
  unresolved external effect remains.

Cancellation before any external write may transition directly to
`CANCELLED`. During provisioning, stop the next step; transition to
`NEEDS_RECONCILIATION` until completed effects are verified or compensated,
then record `CANCELLED`. Starting again requires an explicit command and a new
operation attempt.

Only the application service may transition state. Compare-and-swap on
operation version prevents concurrent transitions. Repeated submission with
the same idempotency key returns the existing operation; reuse with a different
normalized request is rejected. A changed approved spec requires new approval.
Stale worker leases and ambiguous provider responses fail closed.

## Persistence and ownership

Use the same deployment-scoped SQLite database as the Factory lifecycle for
the initial v2 implementation, with additive tables `projects`,
`project_operations`, `project_operation_steps`, and `project_events`.
This keeps identity and operation facts transactionally consistent with task,
run, and workspace records and avoids cross-database transactions. Staging and
production keep separate database paths and service identities. SQLite
transactions remain short and local: persist intent, commit, call the provider
outside the transaction, then persist the verified result in a new transaction.
Never hold a database write lock across network, agent, or process execution.

Recommended constraints:

- registry lifecycle status constrained to `ACTIVE`, `DISABLED`, or `RETIRED`;
- unique `project_id`; unique approved provider/host/owner/repository tuple;
- unique operation idempotency key and unique `(operation_id, step_key)`;
- optimistic `revision` and operation `version`;
- foreign keys from operations and steps to their project/operation;
- allowed-state CHECK constraints and append-only event history;
- immutable `project_id` and repository tuple after first task/run reference;
- task/run records retain their existing project/repository snapshot, so later
  registry edits cannot reroute historical work.

The domain model remains I/O-free. Application services own validation and
transition orchestration through ports. SQLite and GitHub/Trello adapters stay
in infrastructure/integrations. The Control Tower calls the application
service; it never bypasses it with SQL or provider calls.

## Provisioning and security boundaries

- Start with **existing repositories only**. Repository creation or access
  grants are a separately enabled capability and require approval bound to the
  exact owner, name, visibility, and permission diff.
- Use a GitHub App installation scoped to approved organizations/repositories.
  Separate read and write capabilities. Repository metadata/content/PR writes
  are limited to the selected repository and required endpoints. Organization
  repository-administration rights are absent from normal worker credentials.
  If creation needs elevated rights, use a short, operator-triggered credential
  for that step only; never expose it to workers.
- Workers can read approved ProjectSpecs. They cannot create projects, edit the
  registry, grant permissions, change target repositories, merge, deploy, or
  delete repositories.
- Scope Trello access to the configured workspace/board where supported. Failed
  Trello projection does not roll back or corrupt canonical registry state.
- Treat all user/provider input as untrusted. Do not derive paths from project
  names or card titles. Resolve paths beneath configured roots, reject symlink
  escapes, and invoke gates as argv without a shell.
- Show and audit the exact permission/resource diff before approval. Approval
  is tied to request hash, actor, timestamp, and expiry; it cannot authorize an
  edited request.
- Logs/events contain stable IDs, step names, bounded error classes, and
  provider resource IDs only. Never include secrets or raw provider responses.

## Threat model and permission matrix

| Threat | Required control |
|---|---|
| Spoofed project identity from a GitHub issue, URL, or Trello card | Resolve only by exact registered `project_id` and approved repository tuple; reject mismatches. |
| Replayed request or duplicate provider create after restart | Unique idempotency key, persisted step intent, read-after-write verification, and reconciliation on ambiguity. |
| Approval reused after request changes | Approval binds to the normalized spec hash, permission diff, actor, and expiry; any changed hash needs a new approval. |
| Compromised worker attempts registry or infrastructure changes | Worker can read active specs and write only its assigned run workspace/branch; no registry or provisioning credentials. |
| Path traversal or command injection through a project field | Root-contained path resolution, symlink checks, schema allowlists, argv-only bounded gate execution. |
| Credential exposure through UI, provider errors, or logs | Secret references only; redact errors and audit data; never echo provider payloads or credentials. |
| Overbroad provider access | Repository-scoped GitHub App permissions for normal work; elevated repository-creation capability is separate and operator-triggered. |
| Concurrent requests or state races | Unique constraints, operation-version compare-and-swap, leases, and fail-closed conflict handling. |
| Trello drift or hostile card edits | Registry remains canonical; projection is repaired from registry and cannot mutate identity or lifecycle. |

| Actor/component | Allowed | Explicitly denied |
|---|---|---|
| Authenticated operator | Submit request; inspect evidence; approve an exact reviewed operation if authorized | Approve a changed hash implicitly; bypass audit; merge, deploy, or delete through provisioning |
| Control Tower | Submit typed commands through the application service; read persisted status | Own state, write SQL directly, hold provider credentials, call provider APIs directly |
| Provisioning service | Validate, persist transitions, execute allowlisted approved steps | Arbitrary shell, unapproved provider writes, merge/deploy/delete |
| Coding worker | Read active ProjectSpecs; modify its assigned run workspace and branch via existing runtime capabilities | Create/edit registry entries, select another repo/workspace, grant permissions, or access provisioning credentials |
| Trello projection adapter | Read/write projection on the configured board | Define project identity, authorize work, or change canonical status |
| Migration operator | Run redacted inventory/parity and explicitly approved import/cutover | Create provider resources during import or resolve conflicts by guesswork |

## Retry, compensation, and rollback

Each external step follows `intent persisted -> provider call -> read-after-write
verification -> result persisted`. On restart, inspect the step and provider
state with the same stable key before continuing. If the result is ambiguous,
transition to `NEEDS_RECONCILIATION`; do not repeat a non-idempotent create
blindly.

Explicit retry is allowed only from `FAILED` after confirming there are no
unreconciled side effects. `NEEDS_RECONCILIATION` requires an operator to adopt
a verified resource, compensate a reversible step, or abandon with an audit
record. Cancellation stops new steps cooperatively and does not erase completed
side effects.

Rollback is a compensating workflow, not history rewrite. First disable project
eligibility for new tasks, then reverse only verified, reversible
Factory-managed links/hooks/labels with explicit approval. Existing tasks and
workspaces remain intact. Never delete a repository or remove pre-existing
access automatically. If compensation cannot be verified, keep the project
disabled and report `NEEDS_RECONCILIATION`.

## Migration from FACTORY_PROJECTS

Migration is additive and begins with provisioning writes disabled.

1. **Inventory:** parse `FACTORY_PROJECTS` with the current strict parser in
   read-only mode and produce a redacted canonical diff. Never print environment
   values or credentials. Reject malformed JSON, duplicate IDs/repositories,
   unsafe paths, unknown policy fields, and conflicts with project/repository
   identities already persisted on tasks, runs, or backlog links.
2. **Shadow import:** add v2 schema and import validated legacy profiles as
   `ACTIVE` projects with provenance `legacy-factory-projects`; their import
   operation is recorded as complete without replaying provisioning side effects.
   Import is idempotent by project ID and normalized content hash. It creates no
   GitHub/Trello resources.
3. **Parity gate:** in explicit `shadow` mode, resolve through both sources
   and compare normalized profiles. Any missing or differing profile blocks
   cutover; v2 never silently falls back to an unrelated repository.
4. **Read cutover:** explicit deployment configuration selects `legacy`,
   `shadow`, or `registry-v2`. In `registry-v2`, an unavailable, empty, or
   conflicting registry fails closed. Legacy config is retained read-only for
   one rollback window; it is not a live fallback.
5. **Write enablement:** after parity, backup/restore rehearsal, and human
   approval, enable provisioning commands. This gate is independent of read
   cutover. Production migration requires its own approved change window; this
   ADR authorizes no production writes.
6. **Legacy retirement:** after an agreed observation window and proof that
   every active task's project/repository resolves in v2, remove
   `FACTORY_PROJECTS` in a separate release. Preserve an export and rollback
   instructions.

Historical task, run, workspace, PR, and backlog-link rows are not rewritten.
If legacy config disagrees with persisted ownership, migration stops with an
actionable conflict report; an operator resolves it before retrying. No
automatic “best guess” mapping is permitted.

Before provisioning writes are enabled, rollback may switch the explicit mode
back to `legacy` after parity verification. After any v2-only project or
operation exists, first disable affected dispatch, export and reconcile those
records, and use a reviewed restore plan. Never drop v2 tables or delete v2
history during rollback.

## Consequences

Positive: identity and ownership survive restarts; Control Tower, workers, and
Trello have clear authority boundaries; duplicate requests and partial provider
failures are auditable; staging and production remain isolated.

Costs: additive SQLite migration, approval UI, provider capability separation,
reconciliation tooling, and parity checks are required before provisioning is
enabled. The first release supports registration of existing repositories
only; repository creation is a later, separately gated capability.

## Acceptance gates before functional implementation

- Technical review approves this ADR and records accepted deviations.
- Model and state-machine invariants are mapped to tests before implementation.
- Threat review confirms least privilege and approval boundaries for every write.
- Migration dry-run shows exact parity and zero unresolved identity conflicts.
- Failure injection covers duplicate commands, crash between intent/call/result,
  ambiguous provider responses, restart, cancellation, and compensation failure.
- Staging-only E2E proves projects cannot cross-route workspace, branch, gates,
  ownership, or provider writes.
- Production changes remain separately authorized; this ADR is not deployment
  approval.
