"""Auth serializers (design.md §7)."""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.models import Language
from accounts.passwords import password_problems

User = get_user_model()


class UserSerializer(serializers.ModelSerializer[Any]):
    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name", "is_email_verified")
        read_only_fields = fields


def _validate_password_strength(value: str) -> str:
    problems = password_problems(value)
    if problems:
        raise serializers.ValidationError(problems)
    return value


class RegisterSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    workspace_name = serializers.CharField(required=False, allow_blank=True, max_length=120)

    def validate_email(self, value: str) -> str:
        if User.objects.filter(email__iexact=value).exists():
            # Registration cannot hide existence the way login does — the form
            # has to tell the user their address is taken — but the message
            # points at sign-in rather than confirming an account's details.
            raise serializers.ValidationError(
                "An account with this email already exists. Try signing in instead."
            )
        return value.lower()

    def validate_password(self, value: str) -> str:
        return _validate_password_strength(value)


class VerifyEmailSerializer(serializers.Serializer[Any]):
    token = serializers.CharField()


class VerifyCodeSerializer(serializers.Serializer[Any]):
    code = serializers.RegexField(r"^\d{6}$", error_messages={"invalid": "Enter the 6-digit code."})


class PasswordResetRequestSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer[Any]):
    token = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_password(self, value: str) -> str:
        return _validate_password_strength(value)


class SessionSerializer(serializers.Serializer[Any]):
    """The shape returned by register and read by the BFF."""

    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)
    user = UserSerializer(read_only=True)


class PendingEmailSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField()
    expires_at = serializers.DateTimeField()


class DataExportRowSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    state = serializers.CharField()
    sent_to = serializers.EmailField()
    requested_at = serializers.DateTimeField()
    ready_at = serializers.DateTimeField(allow_null=True)


class MeSerializer(serializers.ModelSerializer[Any]):
    """The signed-in person, as every page's shell reads them: lean, because
    `/auth/me/` is fetched on each render of the app."""

    class Meta:
        model = User
        fields: tuple[str, ...] = (
            "id",
            "email",
            "first_name",
            "last_name",
            "is_email_verified",
            "language",
        )
        read_only_fields: tuple[str, ...] = fields


class AccountSerializer(MeSerializer):
    """The signed-in person with what only the account page needs (P2): an
    email change waiting for its link, their recent exports, and the
    organizations they could not leave without an owner."""

    pending_email = serializers.SerializerMethodField()
    exports = serializers.SerializerMethodField()
    sole_owner_of = serializers.SerializerMethodField()

    class Meta(MeSerializer.Meta):
        fields = (*MeSerializer.Meta.fields, "pending_email", "exports", "sole_owner_of")
        read_only_fields = fields

    @extend_schema_field(PendingEmailSerializer(allow_null=True))
    def get_pending_email(self, user: Any) -> dict[str, Any] | None:
        from accounts.services.self_service import pending_email

        return pending_email(user)

    @extend_schema_field(DataExportRowSerializer(many=True))
    def get_exports(self, user: Any) -> list[dict[str, Any]]:
        from accounts.services.self_service import recent_exports

        return DataExportRowSerializer(recent_exports(user), many=True).data  # type: ignore[return-value]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_sole_owner_of(self, user: Any) -> list[str]:
        from accounts.services.self_service import sole_owner_of

        return sole_owner_of(user)


class MeUpdateSerializer(serializers.Serializer[Any]):
    first_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    language = serializers.ChoiceField(choices=Language.choices, required=False)


class PasswordChangeSerializer(serializers.Serializer[Any]):
    current = serializers.CharField(trim_whitespace=False)
    new = serializers.CharField(trim_whitespace=False)


class EmailChangeSerializer(serializers.Serializer[Any]):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)


class EmailConfirmSerializer(serializers.Serializer[Any]):
    token = serializers.CharField()


class AccountDeleteSerializer(serializers.Serializer[Any]):
    confirm = serializers.CharField(allow_blank=True)
