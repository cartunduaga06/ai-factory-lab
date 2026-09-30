"""Compatibility import for the shared reviewed ECC registry."""

from factory.integrations.context.skill_registry import (
    REGISTRY_PATH,
    REGISTRY_SHA256,
    SkillRecord,
    load_registered_skill,
    load_registry,
    parse_registry,
    select_skill,
)

__all__ = [
    "REGISTRY_PATH",
    "REGISTRY_SHA256",
    "SkillRecord",
    "load_registered_skill",
    "load_registry",
    "parse_registry",
    "select_skill",
]
