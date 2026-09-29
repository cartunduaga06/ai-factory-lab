# ECC skill registry spike

This opt-in spike supplies reviewed instruction-only ECC skill prose to Codex
during a Factory code task. Factory still provisions the checkout, dispatches Codex,
executes configured quality gates, binds the validated revision, publishes the
isolated branch, and leaves the pull request at `WAITING_HUMAN`.

## Pin and review

- Upstream: https://github.com/affaan-m/ECC
- License: MIT; the upstream notice is vendored at
  `src/factory/integrations/codex/ecc/LICENSE`.
- Reviewed commit: `c9148d0bb239ed01a95724a5928b98cdf9c30658`.
- Source: `skills/verification-loop/SKILL.md` at that commit.
- Vendored copy: `src/factory/integrations/codex/ecc/verification-loop/SKILL.md`.
- Local SHA-256: `73f62a80e10034274249b94ba611b7c2ade5b729578f168944d763d7bd503c2a`.

`src/factory/integrations/codex/ecc/registry.json` is the versioned Factory-owned
allowlist. Its exact SHA-256 is pinned in `skill_registry.py`; the registry also
pins each skill file. The only reviewed entry currently is `verification-loop`.
The registry schema records name, upstream repository and full commit, source
path, local digest, allowed task types and capabilities, and approval status.
Its sole capability, `code-guidance`, is advisory prose and grants no shell,
sudo, network, production, deploy or cutover authority.

The skill is prose. Its example commands and references to Claude Code hooks are
upstream guidance, not Factory configuration. The adapter does not run those
commands or install hooks. It appends the checked skill to the task instruction
only for `FACTORY_CODEX_ECC_SKILL=verification-loop` and code tasks. With
`FACTORY_CODEX_ECC_SKILL=auto`, one explicit `[type:verification]` title tag
selects that same approved skill. Missing, duplicate, unknown or overlapping
task types fail dispatch closed. Unknown or blocked names, missing files,
symlinks and digest changes also fail dispatch closed. There is
no ECC plugin install, added shell permission, or change to Codex's sandbox.
Factory's configured argv-only gates remain the final validation authority;
Codex's own verification report is advisory.

## Run and reproduce

Set `FACTORY_AGENT_ENGINE=codex` and
`FACTORY_CODEX_ECC_SKILL=verification-loop` for an isolated Factory run. Configure
`FACTORY_QUALITY_GATES` for the target repository as usual. Run
`python -m factory run`. A successful Codex process alone cannot authorize
publication: the required gates must pass against the same workspace revision.
`tests/test_codex_adapter.py` exercises the path with a disposable Codex CLI and
a real local gate in a temporary checkout; the missing-artifact gate fails even
after Codex reports success. This is the safe local end-to-end demonstration.

## Update and rollback

To add a skill, inspect the exact upstream commit and complete file, license,
references, commands and permission implications. Vendor only the reviewed
`SKILL.md` and record its full commit, source path, local SHA-256, task type,
`code-guidance` capability and `APPROVED` status. Give each task type exactly
one approved candidate. Recompute the manifest SHA-256 in `skill_registry.py`,
run the four repository gates and the local end-to-end test, then submit for
human review. Updating a skill follows the same process with a reviewed diff.
Removing one deletes its manifest entry and vendored file together. No runtime
fetch of upstream `main` occurs.

To roll back immediately, unset `FACTORY_CODEX_ECC_SKILL`; later runs use the
ordinary Codex prompt. To roll back the pinned version, revert the skill,
registry, digest, commit and documentation together on a feature branch. Already started
runs retain their dispatched prompt; Factory gates and human review still apply.

## Spike #2 review limit

Only the existing `verification-loop` pin could be verified byte for byte in
this environment. Additional upstream files were not available as trustworthy
local bytes, so no unreviewed skill was added or marked approved. The 3–5 skill
acceptance criterion remains open. A local disposable task → registry → fake
Codex → Factory gate run passed. The full pytest, lint, format and mypy gates
also require the development tools to be installed in the execution workspace.
