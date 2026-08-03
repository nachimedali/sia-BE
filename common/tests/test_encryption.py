"""Encrypted field behaviour (implementation.md Phase 1)."""

from __future__ import annotations

import pytest
from django.db import connection

from common.encryption import get_fernet
from common.tests.models import SecretHolder

pytestmark = pytest.mark.django_db


def _raw_column(pk: int, column: str) -> str | None:
    """Reads the column with the ORM bypassed, so no decryption can occur."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {column} FROM common_tests_secretholder WHERE id = %s",
            [pk],
        )
        row = cursor.fetchone()
    return row[0] if row else None


def test_encrypted_field_roundtrip_and_ciphertext_at_rest() -> None:
    plaintext = "sk_live_do_not_leak_me"
    holder = SecretHolder.objects.create(label="provider", token=plaintext, note="long secret")

    # Roundtrip: the ORM hands back exactly what went in.
    reloaded = SecretHolder.objects.get(pk=holder.pk)
    assert reloaded.token == plaintext
    assert reloaded.note == "long secret"

    # At rest: the column holds ciphertext, not the value.
    stored = _raw_column(holder.pk, "token")
    assert stored is not None
    assert plaintext not in stored
    assert stored.startswith("gAAAAA")  # Fernet's version prefix

    # ...and that ciphertext really does decrypt back to the plaintext.
    assert get_fernet().decrypt(stored.encode()).decode() == plaintext


def test_null_values_are_left_alone() -> None:
    holder = SecretHolder.objects.create(label="empty", token=None, note=None)

    assert SecretHolder.objects.get(pk=holder.pk).token is None
    assert _raw_column(holder.pk, "token") is None


def test_same_plaintext_encrypts_differently_each_time() -> None:
    """Fernet includes a random IV, so equal values must not produce equal rows.

    This is why an encrypted column cannot be filtered or indexed by value —
    anything needing lookup has to carry a separate hash column.
    """
    first = SecretHolder.objects.create(label="a", token="same")
    second = SecretHolder.objects.create(label="b", token="same")

    assert _raw_column(first.pk, "token") != _raw_column(second.pk, "token")
    assert SecretHolder.objects.get(pk=first.pk).token == "same"
    assert SecretHolder.objects.get(pk=second.pk).token == "same"


def test_undecryptable_value_fails_loudly() -> None:
    holder = SecretHolder.objects.create(label="corrupt", token="x")
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE common_tests_secretholder SET token = %s WHERE id = %s",
            ["not-a-fernet-token", holder.pk],
        )

    # Returning garbage — or silently returning the raw column — would be worse
    # than failing: a rotated key must be an incident, not a data corruption.
    with pytest.raises(ValueError, match="FIELD_ENCRYPTION_KEY may have changed"):
        _ = SecretHolder.objects.get(pk=holder.pk).token
