"""Contributor pseudonyms, and the one way a contributor's rows leave the projection.

A contributor is a keyed hash of the workspace id, never the id. The aggregator
needs to count *distinct brands* in a cohort — that is the `N ≥ 8` threshold —
and a hash is enough for counting while being useless for naming anyone: a
dump of the projection without `SECRET_KEY` cannot be joined back to a
workspace. Rotating the key orphans every token, which is harmless because the
projection is derived: the next nightly run rebuilds it under the new key.
"""

from __future__ import annotations

from django.utils.crypto import salted_hmac

from benchmarks.models import BenchmarkObservation

_SALT = "benchmarks.contributor"


def contributor_token(workspace_id: int) -> str:
    return salted_hmac(_SALT, str(workspace_id), algorithm="sha256").hexdigest()


def withdraw(workspace_id: int) -> int:
    """Remove every projected row of this workspace. Returns how many went."""
    deleted, _ = BenchmarkObservation.objects.filter(
        contributor=contributor_token(workspace_id)
    ).delete()
    return deleted
