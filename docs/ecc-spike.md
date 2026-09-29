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
- Reviewed sources and local SHA-256 digests:

  | Task title tag | Upstream source at pinned commit | Local SHA-256 |
  |---|---|---|
  | `[type:verification]` | `skills/verification-loop/SKILL.md` | `73f62a80e10034274249b94ba611b7c2ade5b729578f168944d763d7bd503c2a` |
  | `[type:code-quality]` | `skills/coding-standards/SKILL.md` | `e80544c3d1eac5b14f22022146bd91a6b0ecc30e099cf5181894853aeb81dee1` |
  | `[type:error-handling]` | `skills/error-handling/SKILL.md` | `e8eec6756c950b570486493e64e3bf14c12baf6bf0858bf3928097fe2bfeaf79` |

The two new files were fetched as base64 from the GitHub file API using the full
commit and source paths above. Their decoded upstream bytes and vendored files
have matching SHA-256 digests. `coding-standards` covers naming, readability,
immutability and code quality review; `error-handling` covers typed errors,
retries and failure messages. Review of their complete prose found examples and
advice, with no hook installer or tool permission declaration. The existing
`verification-loop` pin remains byte-for-byte unchanged.

`src/factory/integrations/codex/ecc/registry.json` is the versioned Factory-owned
allowlist. Its exact SHA-256 is pinned in `skill_registry.py`; the registry also
pins each skill file. Exactly three reviewed entries are approved for this spike.
The registry schema records name, upstream repository and full commit, source
path, local digest, allowed task types and capabilities, and approval status.
Its sole capability, `code-guidance`, is advisory prose and grants no shell,
sudo, network, production, deploy or cutover authority.

The skill is prose. Its example commands and references to Claude Code hooks are
upstream guidance, not Factory configuration. The adapter does not run those
commands or install hooks. It appends the checked skill to the task instruction
only for `FACTORY_CODEX_ECC_SKILL=verification-loop` and code tasks. With
`FACTORY_CODEX_ECC_SKILL=auto`, one explicit title tag from the table above
selects its unique approved skill. Missing, duplicate, unknown or overlapping
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
`tests/test_codex_adapter.py` exercises all three automatic selections with a
disposable Codex CLI and a real local gate in a temporary checkout; the
missing-artifact gate fails even after Codex reports success. This is the safe
local end-to-end demonstration.

For this review, the full repository gates completed on the updated branch:
`pytest` (758 passed, 1 skipped), `ruff check .` (passed),
`ruff format --check .` (107 files formatted), and `mypy src` (56 source files,
no issues). Ruff excludes vendored ECC `SKILL.md` files from formatting so its
code-block formatter cannot change the pinned upstream bytes.

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
