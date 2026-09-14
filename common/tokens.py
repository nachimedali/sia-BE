"""Minting and hashing single-use link tokens.

**One implementation, because two would eventually disagree about the hash.**
Phase 2's review links, Phase 6's report shares and anything later that hands
somebody a URL instead of an account all rest on the same three properties:

* the raw token exists **once**, in the message that carries it — never at rest,
  so a database leak cannot be replayed;
* only its digest is stored, and lookup is by digest;
* comparison is constant-time, so a timing signal cannot walk the value out.

Extracted from `collaboration.review`, which minted inline. Reuse rather than a
second copy: a share link whose digest was computed differently would be a
quiet, permanent authentication difference between two surfaces that look
identical.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: 32 bytes of `secrets` entropy, URL-safe. Long enough that guessing is not a
#: threat model, short enough to survive being pasted into a mail client that
#: wraps long lines.
TOKEN_BYTES = 32


def mint() -> tuple[str, str]:
    """`(raw, digest)`. The caller stores the digest and sends the raw once."""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    return raw, digest(raw)


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def matches(raw: str, stored_digest: str) -> bool:
    """Constant-time comparison.

    A plain `==` on a digest leaks how many leading characters were right, and
    a token is exactly the kind of value worth walking out one byte at a time.
    """
    return hmac.compare_digest(digest(raw), stored_digest)
