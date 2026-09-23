"""Command-line entry point for AI Factory Lab.

Phase 1 exposes only a configuration sanity check. This proves the config model
loads from the environment and prints a redacted view — useful for verifying a
local setup without any integration being live.
"""

from __future__ import annotations

import argparse
import json

from factory import __version__
from factory.infrastructure.config import FactoryConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factory", description="AI Factory Lab control plane.")
    parser.add_argument("--version", action="version", version=f"ai-factory-lab {__version__}")
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print the resolved configuration with all credentials masked.",
    )
    args = parser.parse_args(argv)

    if args.show_config:
        config = FactoryConfig.from_env()
        print(json.dumps(config.redacted(), indent=2, sort_keys=True))
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
