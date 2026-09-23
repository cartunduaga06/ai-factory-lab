"""Tests for the structured task source identity.

Identity must be a deterministic triple (provider, repository, issue number),
validated at construction, and never dependent on string parsing.
"""

from __future__ import annotations

import pytest

from factory.domain.models import TaskSource


def test_source_identity_fields_and_display_ref() -> None:
    source = TaskSource(
        provider="github",
        repository_slug="cartunduaga06/ai-factory-lab",
        issue_number=42,
    )
    assert source.provider == "github"
    assert source.repository_slug == "cartunduaga06/ai-factory-lab"
    assert source.issue_number == 42
    assert source.external_ref == "cartunduaga06/ai-factory-lab#42"


def test_source_identity_is_deterministic_and_hashable() -> None:
    a = TaskSource("github", "cartunduaga06/ai-factory-lab", 42)
    b = TaskSource("github", "cartunduaga06/ai-factory-lab", 42)
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


def test_different_providers_are_distinct_identities() -> None:
    github = TaskSource("github", "cartunduaga06/ai-factory-lab", 42)
    gitlab = TaskSource("gitlab", "cartunduaga06/ai-factory-lab", 42)
    assert github != gitlab


def test_different_issue_numbers_are_distinct_identities() -> None:
    first = TaskSource("github", "cartunduaga06/ai-factory-lab", 42)
    second = TaskSource("github", "cartunduaga06/ai-factory-lab", 43)
    assert first != second


def test_provider_must_be_lowercase_ascii_token() -> None:
    with pytest.raises(ValueError):
        TaskSource("", "cartunduaga06/ai-factory-lab", 1)
    with pytest.raises(ValueError):
        TaskSource("   ", "cartunduaga06/ai-factory-lab", 1)
    with pytest.raises(ValueError):
        TaskSource("GitHub", "cartunduaga06/ai-factory-lab", 1)
    with pytest.raises(ValueError):
        TaskSource("github\n", "cartunduaga06/ai-factory-lab", 1)


def test_source_repository_must_be_owner_name() -> None:
    with pytest.raises(ValueError):
        TaskSource("github", "ai-factory-lab", 1)
    with pytest.raises(ValueError):
        TaskSource("github", "", 1)


@pytest.mark.parametrize("number", [0, -1, -42])
def test_issue_number_must_be_positive(number: int) -> None:
    with pytest.raises(ValueError):
        TaskSource("github", "cartunduaga06/ai-factory-lab", number)


def test_external_ref_is_derived_not_authoritative() -> None:
    source = TaskSource("github", "cartunduaga06/ai-factory-lab", 7)
    # Two sources with the same display ref but different providers differ.
    other = TaskSource("gitlab", "cartunduaga06/ai-factory-lab", 7)
    assert source.external_ref == other.external_ref
    assert source != other
