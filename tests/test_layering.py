"""Architecture tests: the layering rules must hold as the factory grows.

These are cheap, deterministic guards on the dependency direction described in
``AGENTS.md`` and ``docs/architecture.md``. They catch the most likely
regression — an import that points the wrong way — without running any code.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "factory"


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _modules(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def _violations(package: str, forbidden_prefix: str) -> list[str]:
    found: list[str] = []
    for path in _modules(package):
        for module in _imports_in(path):
            if module == forbidden_prefix or module.startswith(f"{forbidden_prefix}."):
                found.append(f"{path.relative_to(SRC)} imports {module}")
    return found


def test_domain_imports_no_factory_layer() -> None:
    # The domain is pure: it may not depend on orchestration, integrations or
    # infrastructure.
    violations: list[str] = []
    for layer in ("orchestration", "integrations", "infrastructure"):
        violations += _violations("domain", f"factory.{layer}")
    assert violations == []


def test_domain_has_no_io_imports() -> None:
    forbidden = {"os", "socket", "sqlite3", "urllib", "http", "requests", "pathlib"}
    violations: list[str] = []
    for path in _modules("domain"):
        for module in _imports_in(path):
            root = module.split(".")[0]
            if root in forbidden:
                violations.append(f"{path.relative_to(SRC)} imports {module}")
    assert violations == []


def test_orchestration_does_not_import_integrations_or_infrastructure() -> None:
    violations = _violations("orchestration", "factory.integrations")
    violations += _violations("orchestration", "factory.infrastructure")
    assert violations == []


def test_escalation_free_orchestration_imports_no_concrete_agent_engine() -> None:
    # Orchestration may only see the AgentAdapter protocol, never a concrete
    # engine module (openhands / codex integrations).
    violations: list[str] = []
    for path in _modules("orchestration"):
        for module in _imports_in(path):
            lowered = module.lower()
            if "openhands" in lowered or "codex" in lowered:
                violations.append(f"{path.relative_to(SRC)} imports {module}")
    assert violations == []


def test_infrastructure_does_not_import_orchestration() -> None:
    assert _violations("infrastructure", "factory.orchestration") == []


@pytest.mark.parametrize(
    "package",
    ["domain", "orchestration", "integrations", "infrastructure"],
)
def test_every_package_has_an_init(package: str) -> None:
    assert (SRC / package / "__init__.py").exists()
