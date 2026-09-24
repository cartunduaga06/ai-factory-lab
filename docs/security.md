# Security

AI Factory Lab coordinates autonomous agents that write code. This document
defines the boundaries those agents operate inside. It is policy, not
suggestion: the boundaries are non-negotiable and must be enforced in code as
the factory grows.

## Non-negotiable boundaries

### Agents MAY

- Inspect assigned repositories.
- Create isolated branches and workspaces.
- Edit files within their assigned task.
- Run allowed tests.
- Commit changes.
- Open Pull Requests.

### Agents MAY NOT autonomously

- Merge Pull Requests.
- Push directly to `main` (or any default branch).
- Deploy production.
- Modify production secrets.
- Change GitHub repository permissions.
- Alter unrelated repositories.
- Modify host infrastructure.

**Human approval remains mandatory for merge and production deployment.**

## Why each boundary exists

| Boundary | Risk it prevents |
|---|---|
| No autonomous merge | An agent cannot ship its own unverified change. The PR is a proposal; a human accepts it. |
| No direct push to `main` | Protects the default branch from unreviewed or partially generated work. |
| No production deploy | Keeping deployment out of agent reach means a coding mistake cannot become an outage. |
| No production-secret modification | Limits the blast radius of a compromised or mistaken run. |
| No repository-permission changes | Prevents privilege escalation through the platform rather than the code. |
| No unrelated-repository changes | An agent's authority is scoped to its assigned task. |
| No host-infrastructure changes | The factory must not be able to reconfigure the machine or runtime it depends on. |

The general principle is **least privilege, scoped by task**: an agent has exactly
the access its task needs, for as long as the task runs, and nothing beyond it.

## Enforcement in this repository

Phase 1 establishes the boundaries structurally; later phases add runtime
enforcement.

- **Separation of concerns.** `ai-factory-lab` is the control plane and
  `finanza-ia` is the execution target. Product application code is never copied
  into the factory.
- **Merge is outside the model.** There is no merge operation anywhere in the
  domain or integrations layer. `PullRequest.merged` exists only to record an
  external, human action.
- **Task-scoped workspaces.** `Workspace` is per-run and always on an isolated
  branch (`Workspace.__post_init__` rejects an empty branch). Agents never share
  a working tree.
- **Explicit agent capability surface.** `AgentAdapter` exposes only `dispatch`,
  `collect` and `cancel`. There is no merge, deploy or permission-management
  method to call.
- **Secrets are configuration, not code.** All credentials come from environment
  variables via `FactoryConfig.from_env()`. `.env` is git-ignored; `.env.example`
  contains placeholders only.
- **No credential leakage.** `FactoryConfig.redacted()` masks every credential.
  Logging must use it; raw `asdict()` on config must not be logged. The CLI also
  redacts the configured token from any exception text before printing it.
- **Read-only intake.** `GitHubIssueSource` performs `GET` requests only. It
  never adds or removes labels, closes or reopens issues, comments, changes
  repository settings or permissions, or touches a product repository. The
  `GitHubClient` exposes no write method at all.
- **Write-free eligibility.** Whether an issue is eligible is decided by reading
  its labels; the factory does not label issues to claim them.
- **Duplicate protection is enforced in storage.** The `tasks` table carries
  `UNIQUE(source_provider, source_repository, source_issue_number)`, so a bug in
  application-level checks cannot create two tasks for one issue.
- **One active run per task is enforced in storage.** The `agent_runs` table
  carries a partial unique index over non-terminal statuses, so two concurrent
  dispatchers cannot both record an active run for one task.
- **Engine errors are discarded at the adapter boundary.** `DispatchService`
  converts an adapter failure into `AgentDispatchError` using sanitized
  factory-domain data only (the task id and the persisted run id). The raw engine
  exception is not retained: it is not embedded in the message, and it is not
  chained as `__cause__` or `__context__`. Chaining is itself a leak vector —
  Python renders a chained exception in the traceback, so an engine error string
  that contains a token would reach logs or CLI output through the cause. Because
  the `except` block captures nothing and exits before the sanitized error is
  raised, `AgentDispatchError.__cause__` and `__context__` are both `None`, and no
  formatted traceback contains the engine message. Only sanitized
  factory-domain errors escape orchestration.
- **The failure record carries no engine text.** The terminal `FAILED` run that
  records a failed attempt stores only factory data — task id, adapter kind,
  workspace, status and timestamps — never the engine's exception or summary, so
  a token in an engine message has no persisted path either.
- **Dispatch performs no remote writes.** `DispatchService` calls only the
  `AgentAdapter` protocol. It records the branch a workspace *intends* to use and
  creates nothing on GitHub; branch creation and PR handling are later phases.
- **No execution credential in the factory runtime.** Dispatch consumes no push
  credential. The factory's runtime credential stays read-only for intake, and
  the execution credential remains outside this repository entirely.

## Isolated workspaces and quality gates (Phase 4)

Phase 4 is where the factory first touches the filesystem and runs processes, so
it is where the boundary is easiest to get wrong. Every one of these is enforced
in code:

- **One working tree per run attempt.** A workspace is keyed by its own id
  (`factory/<task-id>/<workspace-id>`), so a retry never inherits another
  attempt's checkout. An agent cannot edit a tree another run owns.
- **The source checkout is never disturbed.** The Git worktree provisioner runs
  only `git worktree add`; it never fetches, pushes, resets, rewrites history, or
  checks out the factory branch in the source repository. The source stays on its
  existing branch.
- **No push, no fetch with credentials, no branch deletion.** The provisioner's
  command surface is deliberately a handful of read-only/creating git calls. It
  holds no push credential, and the factory runtime credential was never granted
  write scope.
- **Git failures are sanitized, not chained.** `WorkspaceProvisioningError`
  carries only the workspace id. Git echoes failing commands and can print a
  remote URL with an embedded token, so the raw stderr and the underlying
  exception are discarded — not attached as `__cause__`/`__context__`, which
  Python would render in a traceback.
- **A failed preparation never fabricates success.** If provisioning fails the
  adapter is not called and no run is recorded; the task stays `CLAIMED` and the
  legal recovery is the existing `BLOCKED`/`CANCELLED` path. No history is
  rewritten to hide the failure.
- **Gates run without a shell.** `LocalQualityGateRunner` executes an argv tuple
  with `shell=False`. Arguments are data, never syntax: metacharacters cannot
  become a second command, and there is no command string to interpolate.
- **Gates run inside the run's workspace,** with `cwd` set to `Workspace.path`.
- **Gates are bounded.** Every execution has a timeout; exceeding it is a
  `FAILED` gate, never a hung factory.
- **Gates receive a minimal environment.** Only a small allowlist of variables is
  forwarded, so a factory credential in the ambient environment is not handed to
  the command.
- **Gate output is never persisted.** `QualityGate.detail` holds only
  `exit_code=<n>`, `timeout` or `spawn_error`. Raw stdout/stderr routinely echo
  environment values, paths and tokens, and the factory cannot know which of them
  is sensitive, so none of it is stored in MVP 0.1.
- **Which gates exist is configuration, not code.** Gate commands come from the
  application layer (`FACTORY_QUALITY_GATES`). The factory never invents a gate,
  and a malformed definition is refused rather than silently ignored — a typo
  cannot disable validation.
- **Validation cannot skip the gate.** A required gate is green only when it
  `PASSED`; `PENDING`, `FAILED` and `SKIPPED` are all not green. An unevaluated
  requirement is never treated as satisfied.

The Phase 4 stop line is itself a safety property: a green validation leaves the
task in `VALIDATING` and **does not** open a PR. Nothing in this phase commits,
pushes, opens a pull request, merges or deploys.

## Credential separation

Two different credentials exist around this project and must never be conflated:

| Credential | Owner | Purpose |
|---|---|---|
| `GITHUB_TOKEN` (factory runtime) | the AI Factory application | READ GitHub Issues during intake |
| The OpenHands execution credential | the development/execution environment | push feature branches and open development PRs |

The factory runtime credential requires no write scope. The execution credential
is used only by the environment that builds the factory; it is never written into
configuration, code, test fixtures, documentation, logs or a git remote URL.

## Secrets handling

- Never commit a real credential. If one is committed, treat it as compromised:
  rotate it first, then remove it from history.
- Never print a secret in full, including in logs, test output or error messages.
- Placeholder values (`<...>`) in `.env.example` are treated as unset by the
  configuration loader, so an accidentally copied example cannot look like valid
  configuration.
- The factory is expected to receive narrowly scoped tokens (a bot account with
  the minimum scopes its tasks require) — not a personal administrator token.
- Intake needs read access to Issues only. A token with write or administration
  scope is over-privileged for this phase.
- Git remotes must not embed credentials. Use a clean
  `https://github.com/<owner>/<repo>.git` remote and let the environment supply
  authentication out of band. An embedded token in `.git/config` is a finding to
  report and sanitize, not a convenience to rely on.

## Human checkpoints

These steps always require a human:

1. **Merge** of any agent-proposed Pull Request.
2. **Production deployment** of anything.
3. **Credential rotation** or creation.
4. **Repository permission or membership changes.**
5. **Changes to host or Docker/OpenHands infrastructure.**

## Reporting a concern

If a change appears to weaken any boundary in this document, stop and raise it
before proceeding. A Pull Request that removes a human checkpoint is a security
regression, not a feature.
