# ECC skill spike: verification-loop

This opt-in spike supplies one instruction-only ECC skill to Codex during a
Factory code task. Factory still provisions the checkout, dispatches Codex,
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

The skill is prose. Its example commands and references to Claude Code hooks are
upstream guidance, not Factory configuration. The adapter does not run those
commands or install hooks. It appends the checked skill to the task instruction
only for `FACTORY_CODEX_ECC_SKILL=verification-loop` and code tasks. Unknown
names, missing files, symlinks and digest changes fail dispatch closed. There is
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

For an update, inspect one new upstream commit and its skill diff, verify its
license and references, replace only the vendored skill, change the commit and
SHA-256 in `ecc_skill.py` and this document, and run all four repository checks
plus the local end-to-end test. Review the resulting PR before enabling the new
pin. Do not fetch `main` at runtime.

To roll back immediately, unset `FACTORY_CODEX_ECC_SKILL`; later runs use the
ordinary Codex prompt. To roll back the pinned version, revert the skill,
digest, commit and documentation together on a feature branch. Already started
runs retain their dispatched prompt; Factory gates and human review still apply.
