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
  Logging must use it; raw `asdict()` on config must not be logged.

## Secrets handling

- Never commit a real credential. If one is committed, treat it as compromised:
  rotate it first, then remove it from history.
- Never print a secret in full, including in logs, test output or error messages.
- Placeholder values (`<...>`) in `.env.example` are treated as unset by the
  configuration loader, so an accidentally copied example cannot look like valid
  configuration.
- The factory is expected to receive narrowly scoped tokens (a bot account with
  the minimum scopes its tasks require) — not a personal administrator token.

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
