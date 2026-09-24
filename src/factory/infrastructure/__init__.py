"""Infrastructure layer: configuration, logging and persistence.

Nothing here may import the orchestration layer. Infrastructure is a leaf: it
supplies capabilities to the layers above it, not the other way around.
"""

from factory.infrastructure.config import (
    AgentConfig,
    DatabaseConfig,
    DatabaseScheme,
    Environment,
    FactoryConfig,
    GitHubConfig,
    LogFormat,
    LoggingConfig,
    UnsupportedDatabaseError,
)
from factory.infrastructure.logging import configure_logging
from factory.infrastructure.persistence import SqliteTaskRepository

__all__ = [
    "AgentConfig",
    "DatabaseConfig",
    "DatabaseScheme",
    "Environment",
    "FactoryConfig",
    "GitHubConfig",
    "LogFormat",
    "LoggingConfig",
    "SqliteTaskRepository",
    "UnsupportedDatabaseError",
    "configure_logging",
]
