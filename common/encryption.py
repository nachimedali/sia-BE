"""Fernet-encrypted model fields (design.md §11).

v1 stores no platform tokens — the publishing provider holds credentials (D2) —
but this is built now because retrofitting encryption onto a populated table is
painful, and the deferred direct-OAuth path (§12) depends on it.

Values are encrypted on the way into the database and decrypted on the way out;
the ciphertext is what is actually stored, which is asserted by
`test_encrypted_field_roundtrip_and_ciphertext_at_rest`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


@lru_cache(maxsize=1)
def get_fernet() -> Fernet:
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
    if not key:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY is not set.")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY must be a url-safe base64-encoded 32-byte key "
            "(generate with `Fernet.generate_key()`)."
        ) from exc


class EncryptedFieldMixin:
    """Transparently encrypts a text-like field.

    Stored as text because the ciphertext is base64. Note the trade-off: an
    encrypted column cannot be filtered, ordered or indexed by value. Anything
    needing lookup must keep a separate hash column.
    """

    def get_internal_type(self) -> str:
        return "TextField"

    def get_prep_value(self, value: Any) -> str | None:
        if value is None:
            return None
        return get_fernet().encrypt(str(value).encode()).decode()

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> Any:
        if value is None:
            return None
        try:
            return get_fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            # Either the key rotated without re-encrypting, or the column holds
            # pre-encryption plaintext. Failing loudly beats returning garbage.
            raise ValueError(
                f"Could not decrypt {self.__class__.__name__} value — "
                "FIELD_ENCRYPTION_KEY may have changed."
            ) from None

    def to_python(self, value: Any) -> Any:
        return value


class EncryptedCharField(EncryptedFieldMixin, models.CharField):  # type: ignore[type-arg]
    pass


class EncryptedTextField(EncryptedFieldMixin, models.TextField):  # type: ignore[type-arg]
    pass
