# Development

How to work in `ai-factory-lab`. Conventions and hard rules also live in
[`../AGENTS.md`](../AGENTS.md); this document covers the day-to-day workflow.

## Requirements

- Python 3.11 or newer (3.13 is what the baseline is verified on).
- `git` and, for GitHub operations, `gh`.

## Setup

```bash
git clone https://github.com/cartunduaga06/ai-factory-lab.git
cd ai-factory-lab

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[dev]"
```

`-e ".[dev]"` installs the package in editable mode plus pytest, Ruff and mypy.

## Configuration

Copy the example and fill in only what you need:

```bash
cp .env.example .env
```

Every integration is optional. With nothing set, defaults are safe: no
credential, `FACTORY_ENV=development`, SQLite for the database URL and
`./.workspaces` for the workspace root.

Verify the resolved configuration without exposing secrets:

```bash
python -m factory --show-config
```

The output masks all credentials (`"***"`). Never commit `.env`.

## Commands

| Purpose | Command |
|---|---|
| Tests | `pytest` |
| Lint | `ruff check .` |
| Format check | `ruff format --check .` |
| Apply formatting | `ruff format .` |
| Types | `mypy` |
| Config sanity check | `python -m factory --show-config` |

`pyproject.toml` sets `testpaths = ["tests"]` and `pythonpath = ["src"]`, so
`pytest` works from the repository root without installing the package.

All four checks (tests, lint, format, types) must pass before a change is
proposed.

## Project layout

```
src/factory/
├── domain/          # pure typed model — no I/O
├── orchestration/   # lifecycle + dispatch
├── integrations/    # GitHub, OpenHands, Codex adapters
└── infrastructure/  # configuration, logging, persistence
tests/               # pure-logic tests, no network or real environment
```

## Layering rules

Dependencies point inward: `infrastructure` and `integrations` depend on
`orchestration`, which depends on `domain`. Never the reverse.

- `domain` must not import from any other `factory` subpackage and must not
  perform I/O.
- `orchestration` must not import a concrete agent engine — only the
  `AgentAdapter` protocol from `domain`.
- `infrastructure` must not import `orchestration`.

If a change seems to require breaking one of these, the design is wrong; raise
it rather than working around it.

## Adding code

1. Start from a feature branch. Never commit to `main`.
2. Put new logic in the layer that owns it (see the table above).
3. Add or update tests for behavior, not implementation details.
4. Run all four checks locally.
5. Open a Pull Request. **Do not merge it.**

## Testing conventions

- Tests cover real logic — no mocks of the code under test.
- No network access and no reading of the real environment. Configuration tests
  pass an explicit mapping to `FactoryConfig.from_env({...})`.
- Keep tests deterministic: no reliance on wall-clock ordering or external state.
- If a required dependency for testing is missing, raise it before installing a
  large stack.

## Change workflow

```bash
git checkout main && git pull
git checkout -b <type>/<short-description>     # e.g. feat/issue-intake

# ... make changes ...

pytest && ruff check . && ruff format --check . && mypy

git add <files> && git commit -m "<clear, imperative message>"
git push -u origin <branch>
gh pr create --fill
```

Then stop. A human reviews and merges. The agent that opened a PR never merges
it.

## Definition of done

- All four checks pass.
- Behavior is covered by tests.
- Layering rules are respected.
- Documentation (`README.md`, `docs/`, `AGENTS.md`) is updated if behavior or
  architecture changed.
- No secret, no product code from `finanza-ia`, and no host-infrastructure change
  is included.
