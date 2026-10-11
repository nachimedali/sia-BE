"""Identity (design.md §6.1).

`User` shipped in Phase 1 because AUTH_USER_MODEL cannot change after the first
migration (design.md A17). `EmailToken` and the auth flows are Phase 2.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from typing import Any, ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models, transaction
from django.utils import timezone

from accounts.managers import UserManager


class User(AbstractUser):
    username = None  # type: ignore[assignment]
    email = models.EmailField("email address", unique=True)
    is_email_verified = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects = UserManager()  # type: ignore[assignment,misc]

    class Meta:
        db_table = "accounts_user"
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.email


class EmailTokenPurpose(models.TextChoices):
    VERIFY = "VERIFY", "Verify email"
    VERIFY_CODE = "VERIFY_CODE", "Verify email by code"
    RESET = "RESET", "Reset password"
    INVITE = "INVITE", "Workspace invitation"


class EmailToken(models.Model):
    """Single-use, expiring tokens delivered by email (design.md §6.1).

    Only a SHA-256 hash of the token is stored. The raw value exists once, in
    the email; a database leak therefore cannot be replayed into account
    takeover. `EmailToken.issue()` is the only way to mint one, and it is the
    only place the raw value is ever available.
    """

    DEFAULT_TTL: ClassVar[dict[str, dt.timedelta]] = {
        EmailTokenPurpose.VERIFY: dt.timedelta(days=3),
        # Short, because six digits is a small space: the attempt cap below
        # and this window together are what make guessing pointless.
        EmailTokenPurpose.VERIFY_CODE: dt.timedelta(minutes=15),
        EmailTokenPurpose.RESET: dt.timedelta(hours=1),
        EmailTokenPurpose.INVITE: dt.timedelta(days=14),
    }

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_tokens"
    )
    purpose = models.CharField(max_length=16, choices=EmailTokenPurpose.choices)
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    #: Wrong guesses against a `VERIFY_CODE` token. Unused by the long tokens,
    #: which cannot be guessed and are looked up by hash instead.
    attempts = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["user", "purpose", "used_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.purpose} for {self.user_id}"

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def _supersede(cls, user: Any, purpose: str) -> None:
        cls.objects.filter(user=user, purpose=purpose, used_at__isnull=True).update(
            used_at=timezone.now()
        )

    @classmethod
    def issue(cls, user: Any, purpose: str) -> tuple[EmailToken, str]:
        """Mints a token, invalidating any outstanding one for the same purpose.

        Superseding matters: without it, a "resend verification" leaves the
        earlier link live, widening the window on an email that may have been
        sent to a mistyped address.
        """
        cls._supersede(user, purpose)

        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            purpose=purpose,
            token_hash=cls.hash_token(raw),
            expires_at=timezone.now() + cls.DEFAULT_TTL[purpose],
        )
        return token, raw

    @classmethod
    def consume(cls, raw: str, purpose: str) -> EmailToken | None:
        """Atomically spends a token. Returns None if unknown, used or expired."""
        token_hash = cls.hash_token(raw)
        with transaction.atomic():
            token = (
                cls.objects.select_for_update()
                .filter(token_hash=token_hash, purpose=purpose)
                .first()
            )
            if token is None or not token.is_usable:
                return None
            token.used_at = timezone.now()
            token.save(update_fields=["used_at"])
            return token

    #: Wrong guesses a code survives. The fifth miss spends it.
    MAX_CODE_ATTEMPTS: ClassVar[int] = 5

    @staticmethod
    def _code_hash(code: str, salt: str) -> str:
        # `salt$digest`, 64 chars, so it fits `token_hash` and stays unique
        # even when two users draw the same six digits.
        return f"{salt}${EmailToken.hash_token(salt + code)[:47]}"

    @classmethod
    def issue_code(cls, user: Any) -> tuple[EmailToken, str]:
        """Mints a six-digit verification code, superseding any earlier one.

        A code is typed rather than clicked, so unlike `issue()` it is looked
        up by user rather than by hash, and carries an attempt count.
        """
        purpose = EmailTokenPurpose.VERIFY_CODE
        cls._supersede(user, purpose)
        code = f"{secrets.randbelow(1_000_000):06d}"
        token = cls.objects.create(
            user=user,
            purpose=purpose,
            token_hash=cls._code_hash(code, secrets.token_hex(8)),
            expires_at=timezone.now() + cls.DEFAULT_TTL[purpose],
        )
        return token, code

    @classmethod
    def consume_code(cls, user: Any, code: str) -> bool:
        """Spends the user's live code if `code` matches it.

        A miss counts against the code and spends it at `MAX_CODE_ATTEMPTS`,
        so the answer to a wrong guess and to a burned code is the same.
        """
        with transaction.atomic():
            token = (
                cls.objects.select_for_update()
                .filter(user=user, purpose=EmailTokenPurpose.VERIFY_CODE, used_at__isnull=True)
                .first()
            )
            if token is None or not token.is_usable:
                return False
            salt = token.token_hash.split("$", 1)[0]
            if hmac.compare_digest(token.token_hash, cls._code_hash(code, salt)):
                token.used_at = timezone.now()
                token.save(update_fields=["used_at"])
                return True
            token.attempts += 1
            if token.attempts >= cls.MAX_CODE_ATTEMPTS:
                token.used_at = timezone.now()
            token.save(update_fields=["attempts", "used_at"])
            return False

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()
