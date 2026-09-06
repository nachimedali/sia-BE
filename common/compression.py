"""Compressed JSON storage for provider payloads (C-07b, P0-39).

A raw response is kept so a normalisation bug is fixed by reprocessing rather
than by re-polling — and for an older post, re-polling is usually impossible:
the quota and the vendor's retention window have both moved on.

Stored compressed because the alternative is a two-year horizon of ~107
captures per target, each carrying a whole provider payload, in a column
something eventually does `SELECT *` over. `zlib` rather than a dependency:
these are small, repetitive JSON objects, which is exactly what deflate is
good at, and the stdlib cannot fall out of the lockfile.
"""

from __future__ import annotations

import json
import zlib
from typing import Any

#: Level 6 is zlib's default. Named rather than implied so a future change is
#: a deliberate edit with a reason, not a silently different corpus.
LEVEL = 6


def pack(payload: Any) -> bytes:
    """JSON → compressed bytes. `None` and `{}` both pack to empty, so an
    absent payload costs nothing and reads back as absent."""
    if payload is None or payload == {}:
        return b""
    return zlib.compress(json.dumps(payload, separators=(",", ":"), default=str).encode(), LEVEL)


def unpack(blob: bytes | memoryview | None) -> Any:
    """Compressed bytes → JSON, or `None` when there was nothing stored.

    Corrupt input returns `None` rather than raising: this is diagnostic data,
    and a reprocessing job that dies on one bad row cannot repair the other
    million. The caller distinguishes "no payload" from "payload said zero"
    through `availability`, never through this.
    """
    if not blob:
        return None
    try:
        return json.loads(zlib.decompress(bytes(blob)).decode())
    except (zlib.error, UnicodeDecodeError, json.JSONDecodeError):
        return None
