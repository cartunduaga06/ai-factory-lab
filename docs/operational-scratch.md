# Operational scratch MVP

The `factory-ready` trigger is unchanged. Add the separate `factory-operational`
label to select the operational path. Classification is persisted as `OPERATIONAL`;
older issues remain `CODE`. The first capability is intentionally limited to
creating a proof file in a private, per-run scratch directory. It does not grant
access to services, Docker, other hosts, production paths, secrets or `sudo`.
Requests for those actions are outside this capability and must remain blocked.

The operator configures `FACTORY_OPERATIONAL_SCRATCH_ROOT` as an existing absolute
directory owned by the non-root Factory service UID, mode `0700`. The Factory
creates one child directory per run. If the root is missing, a symlink, writable
by others, or the process runs as root, provisioning fails closed. Codex runs
with its existing `workspace-write` sandbox and a fixed prompt derived only from
the declaration below. The Issue's free text is not sent as an operational
instruction. No Git branch, commit or PR is created for an operational task.
An operational-only worker needs the GitHub intake credential and control-plane
repository, but no code checkout, target repository or GitHub write credential.
If a `CODE` task reaches that worker, it is blocked before dispatch.

The Issue must contain exactly one fenced `factory-operational` JSON block:

````text
```factory-operational
{"mode":"scratch_artifact","risk":"low","artifact":"proof.txt","payload_hex":"666163746f72792d6f7065726174696f6e616c2d70726f6f660a","sha256":"c6c70f4d31fc460c03b33e98beb093bf6a9531f9ec53b781260b7589201eb82b"}
```
````

`artifact` is a single safe file name. The decoded payload is fixed to
`factory-operational-proof\n`; the declared SHA-256 must match it. Other
payloads and modes are blocked. GitHub intake stores only the canonical,
validated declaration, never surrounding Issue prose. The Factory verifies the resulting regular file,
exact size and checksum after Codex exits successfully. An exit code of zero
without the artifact leaves the task `BLOCKED`; passing both gates moves it
from `VALIDATING` to `DONE`. Gate details, timestamps, engine and a sanitized
process summary (exit code and stdout/stderr byte counts) are persisted. Raw
process output is deleted without logging. Invalid or non-scratch declarations
are blocked before Codex dispatch, with a persisted reason.

This is a local safety and regression harness for future operational adapters.
The approval policy for production mutations requires a separate, explicit
human gate and a stronger host capability; this MVP authorizes no such action.
The repository's offline watcher test exercises intake, Codex worker, scratch
artifact validation and the branchless final state. It does not claim that a
live GitHub Issue or the ai-server host was modified during development.

## OPERATIONAL v0.2 database inspection

An operator may register local SQLite files with
`FACTORY_DATABASE_READONLY_TARGETS`, a JSON object mapping a safe `target_id` to
an absolute file path. A database task declares exactly one fenced block:

````text
```factory-operational
{"mode":"database_readonly","target_id":"pilot"}
```
````

The Factory checks the exact registered id and path through its deny-by-default
policy. It executes the observation itself; no SQL, DSN, path, command or Issue
prose is sent to Codex. The file must be a regular, single-link file without
symlink components or active SQLite journal sidecars. The connection is opened
read-only with query-only and an authorizer. The fixed observations are SQLite
version, integrity check, schema/user version, and counts for at most 32 ordinary
tables. If an `alembic_version` table has exactly one short text revision, its
revision is recorded as a digest. Table names appear only as SHA-256 digests.
The file limit is 64 MiB,
the execution deadline is 2 seconds, and evidence is capped at 2 KiB. Failure
blocks the task with a sanitized reason. Success records bounded evidence and
ends at `DONE` without a branch or PR. This capability does not modify a DB,
perform a backup, or inspect an unregistered target. A separate post-deploy
read-only pilot is required for a real operator target.

## OPERATIONAL v2 boundary

`OperationalCapability` names `scratch`, `database_readonly`, `docker_inspect`,
`service_health`, and `backup`. `OperationalPolicy` defaults to scratch only;
its host, path, command, and target allowlists start empty. The runtime checks
that scratch is enabled before dispatch. A declaration cannot modify this
operator-owned policy. Intake accepts only the fixed scratch or database
declarations plus the fixed Docker inspection declaration. The Codex adapter
accepts only scratch; the Factory-owned database and Docker inspectors are the
only additional executable routes. Backup is always denied
until its route and allowlist design and human gate are implemented and tested.

`docker_inspect` uses `FACTORY_DOCKER_INSPECT_TARGETS`, an operator-owned JSON
map from task `target_id` to an exact container name or full container id. It
uses only the Docker Engine `GET /containers/{id}/json` endpoint through the
Factory-owned read-only proxy socket at `/run/ai-factory/docker-readonly.sock`.
The proxy is an external prerequisite and must itself allow only the inspection
GET route; the Factory never connects directly to the Docker daemon socket. The
executor exposes state/health, image identity, restart count, validated port
numbers, bounded network names, and health-output digests. It never returns
environment values, labels, mounts, raw health output, container IDs, arbitrary
logs, or addresses. Its response is capped at 256 KiB, normalized evidence at
4 KiB, and each request at two seconds. The transport has no mutation method or
Docker CLI passthrough. If the read-only proxy is absent or rejects the request,
inspection fails closed. Unknown target ids fail closed.
`service_health` must read only allowlisted targets. Cutover, webhook, Meta, and
destructive actions have no capability. Until those executors and their offline E2E tests exist,
requests for the remaining modes fail closed at intake or dispatch.

Rollback is to stop the worker, restore the previous release, and leave any
blocked task for an explicit retry after correcting policy. The scratch artifact
is disposable under its per-run directory. The database inspector makes no
database writes; Docker inspection issues GET only, and service and Docker state is unchanged.
