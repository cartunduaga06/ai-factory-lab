"""Datetime codec shared by the SQLite repositories.

Timestamps are stored as ISO-8601 text. Keeping the two functions in one place
means every table encodes and decodes time identically, so a round-trip never
depends on which repository wrote the row.
"""

from __future__ import annotations

from datetime import datetime


def encode_datetime(value: datetime) -> str:
    """Serialize ``value`` to ISO-8601 text."""
    return value.isoformat()


def decode_datetime(raw: str) -> datetime:
    """Parse ISO-8601 text written by :func:`encode_datetime`."""
    return datetime.fromisoformat(raw)


__all__ = ["decode_datetime", "encode_datetime"]
