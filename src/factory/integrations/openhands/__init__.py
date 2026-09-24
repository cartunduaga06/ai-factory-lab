"""OpenHands integration.

OpenHands is a supported agent *execution engine*. This package is the only
place in the factory that knows OpenHands' HTTP contract; it translates that
contract into factory domain types (``FactoryTask`` -> conversation,
``execution_status`` -> ``RunStatus``). The orchestration layer sees only the
``AgentAdapter`` protocol, never this package.
"""

from factory.integrations.openhands.adapter import OpenHandsAdapter
from factory.integrations.openhands.client import (
    OpenHandsClient,
    OpenHandsConfigurationError,
    OpenHandsConnectionError,
    OpenHandsError,
    OpenHandsRequestError,
    OpenHandsResponseError,
    OpenHandsStatusError,
    ServerResponse,
    Transport,
    UrllibTransport,
)
from factory.integrations.openhands.execution import (
    DEFAULT_MAX_ITERATIONS,
    OpenHandsExecution,
    build_instruction,
)
from factory.integrations.openhands.status import (
    OpenHandsExecutionStatus,
    map_status,
)

__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "OpenHandsAdapter",
    "OpenHandsClient",
    "OpenHandsConfigurationError",
    "OpenHandsConnectionError",
    "OpenHandsError",
    "OpenHandsExecution",
    "OpenHandsExecutionStatus",
    "OpenHandsRequestError",
    "OpenHandsResponseError",
    "OpenHandsStatusError",
    "ServerResponse",
    "Transport",
    "UrllibTransport",
    "build_instruction",
    "map_status",
]
