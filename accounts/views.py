"""Auth endpoints (design.md §7).

Views parse and serialise; `accounts/services/auth.py` decides
(implementation.md §4.1).
"""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from accounts.models import User
from accounts.serializers import (
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    SessionSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)
from accounts.services import auth as auth_service
from common.exceptions import OCCSError
from common.throttling import (
    LoginThrottle,
    PasswordResetThrottle,
    RegisterThrottle,
    VerificationResendThrottle,
)


def issue_session(user: Any) -> dict[str, Any]:
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh), "user": user}


class RegisterView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [RegisterThrottle]

    @extend_schema(
        request=RegisterSerializer,
        responses={201: SessionSerializer},
        summary="Create an account",
        description=(
            "Creates the user, a workspace, an OWNER membership and assigns the Free plan, "
            "then queues a verification email. Returns a session: the user is signed in but "
            "unverified, and onboarding step 1 gates progress until they verify."
        ),
        auth=[],
    )
    def post(self, request: Request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user, _workspace = auth_service.register(
            email=serializer.validated_data["email"],
            password=serializer.validated_data["password"],
            workspace_name=serializer.validated_data.get("workspace_name") or None,
        )
        return Response(SessionSerializer(issue_session(user)).data, status=status.HTTP_201_CREATED)


class VerifyEmailView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]

    @extend_schema(
        request=VerifyEmailSerializer,
        responses={200: None, 400: None},
        summary="Verify an email address",
        auth=[],
    )
    def post(self, request: Request) -> Response:
        serializer = VerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not auth_service.verify_email(serializer.validated_data["token"]):
            # Raised rather than hand-built so it goes through the one handler
            # that owns the envelope (design.md A3).
            raise OCCSError(
                "This verification link is invalid or has expired.", code="invalid_token"
            )
        return Response({"verified": True})


class ResendVerificationView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]
    throttle_classes: list[Any] = [VerificationResendThrottle]

    @extend_schema(
        request=None,
        responses={202: None},
        summary="Resend the verification email",
        description="Always 202 — an already-verified account is a no-op.",
    )
    def post(self, request: Request) -> Response:
        auth_service.resend_verification(cast(User, request.user))
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)


class PasswordResetRequestView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [PasswordResetThrottle]

    @extend_schema(
        request=PasswordResetRequestSerializer,
        responses={202: None},
        summary="Request a password reset",
        description=(
            "Always 202, whether or not the address exists — otherwise the endpoint "
            "is an account-enumeration oracle."
        ),
        auth=[],
    )
    def post(self, request: Request) -> Response:
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        auth_service.request_password_reset(serializer.validated_data["email"])
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)


class PasswordResetConfirmView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [PasswordResetThrottle]

    @extend_schema(
        request=PasswordResetConfirmSerializer,
        responses={200: None, 400: None},
        summary="Set a new password from a reset link",
        auth=[],
    )
    def post(self, request: Request) -> Response:
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        ok = auth_service.confirm_password_reset(
            serializer.validated_data["token"], serializer.validated_data["password"]
        )
        if not ok:
            raise OCCSError("This reset link is invalid or has expired.", code="invalid_token")
        return Response({"reset": True})


class MeView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(responses={200: UserSerializer}, summary="The signed-in user")
    def get(self, request: Request) -> Response:
        return Response(UserSerializer(request.user).data)


class ThrottledTokenObtainPairView(TokenObtainPairView):
    """Login, with the per-IP bucket attached.

    simplejwt's view has no throttling of its own, which would leave the single
    most attacked endpoint in the product unprotected.
    """

    throttle_classes: list[Any] = [LoginThrottle]
