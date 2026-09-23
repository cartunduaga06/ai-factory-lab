"""Logging setup for AI Factory Lab.

Kept deliberately small: a single ``configure_logging`` entry point. Nothing in
this package logs configuration values — callers pass
:meth:`factory.infrastructure.config.FactoryConfig.redacted` if they need to.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

from factory.infrastructure.config import LogFormat, LoggingConfig

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"


def configure_logging(config: LoggingConfig, stream: TextIO | None = None) -> None:
    """Configure the root logger from ``config``.

    ``stream`` defaults to ``stderr`` and exists mainly so tests can capture
    output deterministically.
    """
    formatter = logging.Formatter(LOG_FORMAT)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.getLevelName(config.level))

    if config.fmt is LogFormat.JSON:
        # Structured JSON logging is planned; downgrade loudly rather than
        # silently emitting a format consumers did not ask for.
        root.warning("FACTORY_LOG_FORMAT=json is not implemented yet; using text output")


__all__ = ["LOG_FORMAT", "configure_logging"]
