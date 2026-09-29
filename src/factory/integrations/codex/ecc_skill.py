"""Load the single reviewed ECC instruction from Factory-owned package data."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

SKILL_NAME = "verification-loop"
UPSTREAM_COMMIT = "c9148d0bb239ed01a95724a5928b98cdf9c30658"
SKILL_SHA256 = "73f62a80e10034274249b94ba611b7c2ade5b729578f168944d763d7bd503c2a"
_SKILL_PATH = Path(__file__).parent / "ecc" / SKILL_NAME / "SKILL.md"


def load_skill(name: str, *, path: Path = _SKILL_PATH) -> str:
    """Return the pinned instruction, rejecting unknown names or changed bytes."""
    if name != SKILL_NAME:
        raise ValueError("unsupported ECC skill")
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("ECC skill is not a regular file")
        content = path.read_bytes()
    except OSError as exc:
        raise ValueError("ECC skill is unavailable") from exc
    if hashlib.sha256(content).hexdigest() != SKILL_SHA256:
        raise ValueError("ECC skill digest mismatch")
    return content.decode("utf-8")
