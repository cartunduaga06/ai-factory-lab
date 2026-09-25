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
    forbidden = {
        "os",
        "socket",
        "sqlite3",
        "subprocess",
        "git",
        "urllib",
        "http",
        "requests",
        "pathlib",
        "shutil",
        "tempfile",
    }
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


def test_openhands_lives_only_in_integrations() -> None:
    # The concrete OpenHands integration may not be referenced from the domain or
    # the orchestration layer: those layers depend on the AgentAdapter protocol,
    # never a concrete engine package.
    violations: list[str] = []
    for layer in ("domain", "orchestration"):
        violations += _violations(layer, "factory.integrations.openhands")
    assert violations == []


def test_no_layer_imports_the_openhands_sdk() -> None:
    # The factory talks to the agent server over HTTP, not by importing OpenHands.
    violations: list[str] = []
    for layer in ("domain", "orchestration", "integrations", "infrastructure"):
        for path in _modules(layer):
            for module in _imports_in(path):
                if module == "openhands" or module.startswith("openhands."):
                    violations.append(f"{path.relative_to(SRC)} imports {module}")
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


def test_dispatch_depends_only_on_domain_contracts() -> None:
    # The dispatch service is the execution seam: it may see the AgentAdapter
    # protocol and the domain ports, but no storage engine and no concrete agent
    # integration.
    modules = _imports_in(SRC / "orchestration" / "dispatch.py")
    forbidden = {
        module
        for module in modules
        if module.startswith(("factory.integrations", "factory.infrastructure", "sqlite3"))
    }
    assert forbidden == set()


def test_orchestration_imports_no_process_or_git_modules() -> None:
    # Concrete git/workspace and process execution live outside orchestration.
    forbidden = {"subprocess", "sqlite3", "shutil"}
    violations: list[str] = []
    for path in _modules("orchestration"):
        for module in _imports_in(path):
            root = module.split(".")[0]
            if root in forbidden or root == "git" or module.startswith("factory.infrastructure"):
                violations.append(f"{path.relative_to(SRC)} imports {module}")
    assert violations == []


def test_workspace_and_gate_integrations_live_outside_core_layers() -> None:
    # The concrete implementations exist, and exist only under integrations.
    assert (SRC / "integrations" / "workspace" / "git.py").exists()
    assert (SRC / "integrations" / "gates" / "local.py").exists()
    for layer in ("domain", "orchestration"):
        violations = _violations(layer, "factory.integrations.workspace")
        violations += _violations(layer, "factory.integrations.gates")
        assert violations == []


def test_ports_define_the_phase_4_contracts() -> None:
    # The smallest engine-agnostic seams Phase 4 needs are declared as ports.
    from factory.domain import ports

    assert hasattr(ports, "WorkspaceProvisioner")
    assert hasattr(ports, "QualityGateRunner")
    assert hasattr(ports.RunRepository, "update_run")


def test_ports_define_the_phase_5_contracts() -> None:
    # Publication ports live in the domain, so orchestration never imports
    # factory.integrations to see the PullRequestSink contract.
    from factory.domain import ports

    assert hasattr(ports, "WorkspacePublisher")
    assert hasattr(ports, "PullRequestSink")
    assert hasattr(ports, "PullRequestRepository")


def test_pull_request_sink_is_accessible_without_orchestration_importing_integrations() -> None:
    # The contract can be reached from the domain boundary alone.
    from factory.domain.ports import PullRequestSink

    assert hasattr(PullRequestSink, "find_open_pull_request")
    assert hasattr(PullRequestSink, "open_pull_request")
    # And there is deliberately no merge capability anywhere in the contract.
    assert not hasattr(PullRequestSink, "merge_pull_request")
    assert not hasattr(PullRequestSink, "merge")


def test_concrete_publication_components_live_outside_core_layers() -> None:
    assert (SRC / "integrations" / "workspace" / "git_publish.py").exists()
    assert (SRC / "integrations" / "github" / "pull_requests.py").exists()
    assert (SRC / "integrations" / "github" / "write_client.py").exists()
    for layer in ("domain", "orchestration"):
        violations = _violations(layer, "factory.integrations.github")
        violations += _violations(layer, "factory.integrations.workspace.git_publish")
        assert violations == []


def test_publication_module_imports_no_concrete_implementation() -> None:
    # PublicationService is the Phase 5 orchestration seam: it may see only domain
    # ports, never GitHub, OpenHands, git, subprocess or SQLite.
    modules = _imports_in(SRC / "orchestration" / "publication.py")
    forbidden = {
        module
        for module in modules
        if module.startswith(
            (
                "factory.integrations",
                "factory.infrastructure",
                "sqlite3",
                "subprocess",
                "git",
            )
        )
    }
    assert forbidden == set()


def test_domain_has_no_merge_or_github_write_capability() -> None:
    # The domain model must not grow a merge or deploy capability.
    from factory.domain import models

    for name in ("merge_pull_request", "merge_pr", "deploy"):
        assert not hasattr(models.PullRequest, name)


@pytest.mark.parametrize(
    "package",
    ["domain", "orchestration", "integrations", "infrastructure"],
)
def test_every_package_has_an_init(package: str) -> None:
    assert (SRC / package / "__init__.py").exists()
