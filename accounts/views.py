"""Auth endpoints (design.md §7).

Views parse and serialise; `accounts/services/auth.py` decides
(implementation.md §4.1).
"""

from __future__ import annotations

from typing import Any, cast

from django.http import FileResponse
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
    AccountDeleteSerializer,
    AccountSerializer,
    EmailChangeSerializer,
    EmailConfirmSerializer,
    MeSerializer,
    MeUpdateSerializer,
    PasswordChangeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    SessionSerializer,
    VerifyCodeSerializer,
    VerifyEmailSerializer,
)
from accounts.services import auth as auth_service
from accounts.services import self_service
from common.exceptions import NotFoundError, OCCSError
from common.throttling import (
    LoginThrottle,
    PasswordResetThrottle,
    RegisterThrottle,
    VerificationCodeThrottle,
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


class VerifyCodeView(APIView):
    """Onboarding step 1: the six-digit code from the verification email.

    Needs the session registration issued — a code alone is a small space, and
    tying it to the signed-in account is what keeps it from being sprayed
    across everyone's.
    """

    permission_classes: list[Any] = [IsAuthenticated]
    throttle_classes: list[Any] = [VerificationCodeThrottle]

    @extend_schema(
        request=VerifyCodeSerializer,
        responses={200: None, 400: None, 404: None},
        summary="Verify the account's email with the emailed code",
        description=(
            "400 `invalid_code` for a wrong, expired, spent or burned code — one answer, "
            "so a guess learns nothing. Five wrong tries spend the code. 404 while the "
            "transactional-email rollout is off."
        ),
    )
    def post(self, request: Request) -> Response:
        user = cast(User, request.user)
        if not auth_service.transactional_email_enabled(user):
            raise NotFoundError("This endpoint does not exist.")
        serializer = VerifyCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not auth_service.verify_code(user, serializer.validated_data["code"]):
            raise OCCSError(
                "That code is wrong or has expired. Request a new one.", code="invalid_code"
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
    """The signed-in person's own account (P2): read, rename, pick a language,
    and delete. Company settings live on the workspace, never here."""

    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(responses={200: MeSerializer}, summary="The signed-in user")
    def get(self, request: Request) -> Response:
        return Response(MeSerializer(request.user).data)

    @extend_schema(
        request=MeUpdateSerializer,
        responses={200: AccountSerializer},
        summary="Rename, or pick a language",
    )
    def patch(self, request: Request) -> Response:
        payload = MeUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        user = cast(User, request.user)
        for field, value in payload.validated_data.items():
            setattr(user, field, value)
        user.save(update_fields=[*payload.validated_data, "updated_at"])
        return Response(AccountSerializer(user).data)

    @extend_schema(
        request=AccountDeleteSerializer,
        responses={204: None},
        summary="Delete your account",
        description="Needs `confirm` = `DELETE <your email>`. 409 `sole_owner` while you are "
        "the only owner of an organization others work in; 409 `active_subscription` while a "
        "paid plan runs.",
    )
    def delete(self, request: Request) -> Response:
        payload = AccountDeleteSerializer(data=request.data or {})
        payload.is_valid(raise_exception=True)
        self_service.delete_account(
            cast(User, request.user), confirm=payload.validated_data["confirm"]
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AccountView(APIView):
    """`GET /auth/me/account/`: the account page's read (P2) — `/auth/me/`
    plus pending email, exports and sole ownerships, kept off the shell's
    per-page read."""

    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(responses={200: AccountSerializer}, summary="Your account page")
    def get(self, request: Request) -> Response:
        return Response(AccountSerializer(request.user).data)


class MePasswordView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]
    throttle_classes: list[Any] = [PasswordResetThrottle]

    @extend_schema(
        request=PasswordChangeSerializer,
        responses={200: SessionSerializer},
        summary="Change your password",
        description="Needs the current password. Signs out every session, this one too, "
        "and answers with its replacement.",
    )
    def post(self, request: Request) -> Response:
        payload = PasswordChangeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        user = cast(User, request.user)
        self_service.change_password(
            user, current=payload.validated_data["current"], new=payload.validated_data["new"]
        )
        return Response(SessionSerializer(issue_session(user)).data)


class MeEmailView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]
    throttle_classes: list[Any] = [PasswordResetThrottle]

    @extend_schema(
        request=EmailChangeSerializer,
        responses={202: AccountSerializer},
        summary="Ask to change your email",
        description="Sends a link to the new address; the email changes only when it is clicked.",
    )
    def post(self, request: Request) -> Response:
        payload = EmailChangeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        user = self_service.request_email_change(
            cast(User, request.user),
            email=payload.validated_data["email"],
            password=payload.validated_data["password"],
        )
        return Response(AccountSerializer(user).data, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        request=None, responses={200: AccountSerializer}, summary="Cancel a pending email change"
    )
    def delete(self, request: Request) -> Response:
        return Response(
            AccountSerializer(self_service.cancel_email_change(cast(User, request.user))).data
        )


class EmailConfirmView(APIView):
    """The link in the email sent to a new address (P2). No session needed:
    the token is the credential, single-use and a day long."""

    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]
    throttle_classes: list[Any] = [PasswordResetThrottle]

    @extend_schema(
        request=EmailConfirmSerializer,
        responses={200: None},
        summary="Confirm a new email",
        auth=[],
    )
    def post(self, request: Request) -> Response:
        payload = EmailConfirmSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        user = self_service.confirm_email_change(payload.validated_data["token"])
        return Response({"email": user.email})


class MeExportView(APIView):
    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(
        request=None,
        responses={202: AccountSerializer},
        summary="Email me a copy of my data",
        description="Built in the background; 409 while one is already being prepared.",
    )
    def post(self, request: Request) -> Response:
        user = cast(User, request.user)
        self_service.request_export(user)
        return Response(AccountSerializer(user).data, status=status.HTTP_202_ACCEPTED)


class MeExportDownloadView(APIView):
    """Your own export, within its 7 days. Anyone else's, or an old one, is 404."""

    permission_classes: list[Any] = [IsAuthenticated]

    @extend_schema(responses={(200, "application/json"): bytes}, summary="Download a data export")
    def get(self, request: Request, pk: int) -> FileResponse:
        export = self_service.downloadable(cast(User, request.user), pk)
        if export is None:
            raise NotFoundError("No such export.", code="export_not_found")
        return FileResponse(
            export.file.open("rb"),
            as_attachment=True,
            filename=f"composevision-data-{export.requested_at:%Y-%m-%d}.json",
            content_type="application/json",
        )


class ThrottledTokenObtainPairView(TokenObtainPairView):
    """Login, with the per-IP bucket attached.

    simplejwt's view has no throttling of its own, which would leave the single
    most attacked endpoint in the product unprotected.
    """

    throttle_classes: list[Any] = [LoginThrottle]
