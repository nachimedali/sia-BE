"""Append-only records.

Two kinds of row in this system may never be edited after they are written: the
ledgers (I4 — they are the evidence in a billing dispute) and the analytics
captures (a statement about a moment, and the only thing an engagement decay
slope can be computed from).

They share the guard and the exception but nothing else — `LedgerEntry` carries
workspace/delta/balance columns a metric has no use for — so what is shared is
exactly this mixin. One error class, so a caller catching "this row cannot be
mutated" never has to know which kind of row said so.
"""

from __future__ import annotations

from typing import Any

from django.db import models


class AppendOnlyError(RuntimeError):
    """Raised when something tries to mutate an append-only row."""


class AppendOnly(models.Model):
    """Refuses every write after the first, and every delete.

    Subclasses set `append_only_hint` to say what the caller should do instead —
    a ledger wants a compensating row, a capture wants another capture — because
    "you cannot edit this" without "here is the supported way" is a dead end.
    """

    #: Appended to the error message. Subclasses override.
    append_only_hint = "write another row instead of editing this one."

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AppendOnlyError(
                f"{type(self).__name__} is append-only; {self.append_only_hint}"
            )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        raise AppendOnlyError(f"{type(self).__name__} is append-only and cannot be deleted.")
