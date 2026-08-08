"""Auth serializers (design.md §7)."""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

User = get_user_model()


class UserSerializer(serializers.ModelSerializer[Any]):
    class Meta:
        model = User
        fields = ("id", "email", "first_name", "last_name", "is_email_verified")
        read_only_fields = fields


def _validate_password_strength(value: str) -> str:
    try:
        validate_password(value)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(list(exc.messages)) from exc
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
