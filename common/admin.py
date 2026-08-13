"""Admin plumbing shared across apps.

Three apps now hold records that must not be edited by hand — billing's ledgers
(I4), the trend corpus, and the analytics captures — and each had written the
same two things. One copy, so a Django signature change or a new rule about
read-only records is one edit rather than three.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.db import models
from django.http import HttpRequest


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """No add, no change, no delete. For records the application owns.

    The ledgers are the evidence in a billing dispute; a screen that can edit
    them is a screen that can lose the dispute. Pipeline output is the same
    argument in a smaller key — editing a score by hand puts the corpus out of
    step with the formula that produced it, and the honest way to change what it
    says is to re-run the pipeline.
    """

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


def all_fields_except_id(model: type[models.Model]) -> tuple[str, ...]:
    """Every field but the pk, for `readonly_fields`.

    Listed wholesale rather than picked one by one so a column added later is
    locked automatically — the failure mode of the explicit list is a new field
    that is silently editable.
    """
    return tuple(field.name for field in model._meta.fields if field.name != "id")
