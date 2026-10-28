"""Brand avatar endpoints (S17). Views parse and serialise;
`brand.services.avatars` decides. Every lookup is scoped to the request's
workspace, so another brand's avatar or draft is a 404 (rule 3); with
`ad_templates_s17` off the routes do not exist."""

from __future__ import annotations

from typing import Any

from django.db.models import Count
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.models import CreativeKind, CreativeOption
from billing.permissions import HasFlag
from billing.services.entitlements import entitlements_for
from billing.services.flags import AD_TEMPLATES_S17
from brand.models import Avatar, AvatarDraft, AvatarKind
from brand.services import avatars
from common.workspaces import authenticated_user, request_workspace
from content.models import MediaAsset
from workspaces.models import Permission, Workspace
from workspaces.permissions import HasPermission, caller_permissions

_READ: list[Any] = [IsAuthenticated, HasFlag(AD_TEMPLATES_S17), HasPermission(Permission.VIEW)]
_WRITE: list[Any] = [IsAuthenticated, HasFlag(AD_TEMPLATES_S17), HasPermission(Permission.EDIT)]
_ADMIN: list[Any] = [IsAuthenticated, HasFlag(AD_TEMPLATES_S17), HasPermission(Permission.ADMIN)]


class AvatarMediaSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    url = serializers.CharField(allow_null=True)


class AvatarConsentSerializer(serializers.Serializer[Any]):
    signed_by = serializers.CharField()
    signed_on = serializers.DateField()
    revoked_at = serializers.DateTimeField(allow_null=True)


class AvatarSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField()
    kind = serializers.ChoiceField(choices=AvatarKind.choices)
    portrait = AvatarMediaSerializer()
    angles = AvatarMediaSerializer(many=True)
    traits = serializers.DictField(child=serializers.CharField())
    languages = serializers.ListField(child=serializers.CharField())
    is_house = serializers.BooleanField()
    consent = AvatarConsentSerializer(allow_null=True)
    #: `usable`, or why not: `over_limit`, `consent_revoked`,
    #: `consent_required`, `plan`, `archived`.
    state = serializers.CharField()
    renders = serializers.IntegerField()
    created_at = serializers.DateTimeField()


class AvatarLimitsSerializer(serializers.Serializer[Any]):
    max_avatars = serializers.IntegerField()
    used = serializers.IntegerField()
    drafts_monthly = serializers.IntegerField()
    drafts_left = serializers.IntegerField()
    real_avatars = serializers.BooleanField()
    suggested_plan = serializers.CharField()
    real_suggested_plan = serializers.CharField()
    #: Credits one batch of portraits costs.
    draft_credits = serializers.IntegerField()


class AvatarTraitSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]
    group = serializers.CharField()
    values = serializers.DictField(child=serializers.CharField(), required=False)


class AvatarLibrarySerializer(serializers.Serializer[Any]):
    avatars = AvatarSerializer(many=True)
    limits = AvatarLimitsSerializer()
    traits = AvatarTraitSerializer(many=True)


class AvatarCreateSerializer(serializers.Serializer[Any]):
    """An invented avatar names a ready `draft` and the `index` of the portrait;
    a real one carries 3 to 5 `photos` and the `consent`."""

    kind = serializers.ChoiceField(choices=["synthetic", "real"], default="synthetic")
    name = serializers.CharField(max_length=avatars.NAME_MAX)
    languages = serializers.ListField(
        child=serializers.CharField(max_length=4), required=False, default=list
    )
    is_house = serializers.BooleanField(required=False, default=False)
    draft = serializers.IntegerField(required=False, allow_null=True, default=None)
    index = serializers.IntegerField(required=False, min_value=0, default=0)
    photos = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    signed_by = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    signed_on = serializers.DateField(required=False, allow_null=True, default=None)


class AvatarDraftRequestSerializer(serializers.Serializer[Any]):
    traits = serializers.JSONField()


class AvatarDraftPortraitSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    url = serializers.CharField(allow_null=True)


class AvatarDraftSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    status = serializers.CharField()
    traits = serializers.DictField(child=serializers.CharField())
    portraits = AvatarDraftPortraitSerializer(many=True)
    credits = serializers.IntegerField()
    charged = serializers.IntegerField()
    error = serializers.CharField(allow_blank=True)
    #: Served from unused faces of earlier batches: no model call, no credits.
    reused = serializers.BooleanField()


def _urls(workspace: Workspace, ids: list[int]) -> dict[int, str | None]:
    assets = MediaAsset.objects.filter(workspace=workspace, pk__in=ids)
    return {asset.pk: (asset.file.url if asset.file else None) for asset in assets}


def _avatar_rows(workspace: Workspace, rows: list[Avatar]) -> list[dict[str, Any]]:
    ids = [pk for avatar in rows for pk in (avatar.portrait_id, *avatar.angles)]
    urls = _urls(workspace, ids)
    entitlements = entitlements_for(workspace)
    usable = avatars.usable_ids(workspace, avatars.cap(workspace, entitlements))
    real_allowed = bool(entitlements.feature("real_avatars"))
    counts = dict(
        Avatar.objects.filter(pk__in=[a.pk for a in rows])
        .annotate(n=Count("video_renders"))
        .values_list("pk", "n")
    )
    return [
        {
            "id": avatar.pk,
            "name": avatar.name,
            "kind": avatar.kind,
            "portrait": {"id": avatar.portrait_id, "url": urls.get(avatar.portrait_id)},
            "angles": [{"id": pk, "url": urls.get(pk)} for pk in avatar.angles],
            "traits": avatar.traits,
            "languages": avatar.languages,
            "is_house": avatar.is_house,
            "consent": (
                {
                    "signed_by": avatar.consent_signed_by,
                    "signed_on": avatar.consent_signed_on,
                    "revoked_at": avatar.consent_revoked_at,
                }
                if avatar.kind == AvatarKind.REAL and avatar.consent_signed_on
                else None
            ),
            "state": avatars.state_of(avatar, usable, real_allowed=real_allowed),
            "renders": counts.get(avatar.pk, 0),
            "created_at": avatar.created_at,
        }
        for avatar in rows
    ]


def _one(workspace: Workspace, avatar: Avatar) -> dict[str, Any]:
    return dict(AvatarSerializer(_avatar_rows(workspace, [avatar])[0]).data)


def _draft(workspace: Workspace, draft: AvatarDraft) -> dict[str, Any]:
    urls = _urls(workspace, [p["media"] for p in draft.portraits])
    return dict(
        AvatarDraftSerializer(
            {
                "id": draft.pk,
                "status": draft.status,
                "traits": draft.traits,
                "portraits": [
                    {"id": p["media"], "url": urls.get(p["media"])} for p in draft.portraits
                ],
                "credits": draft.credits,
                "charged": draft.credits if draft.charge_id else 0,
                "error": draft.error,
                "reused": draft.reused,
            }
        ).data
    )


class AvatarListView(APIView):
    def get_permissions(self) -> list[Any]:
        return [p() for p in (_WRITE if self.request.method == "POST" else _READ)]

    @extend_schema(
        responses={200: AvatarLibrarySerializer},
        summary="The brand's avatars, its limits and the traits to describe a new one",
        description="Archived avatars are left out; locked ones are listed with their state.",
    )
    def get(self, request: Request) -> Response:
        workspace = request_workspace(request)
        rows = list(
            Avatar.objects.filter(workspace=workspace, archived_at__isnull=True).order_by(
                "-is_house", "created_at", "id"
            )
        )
        traits = CreativeOption.objects.filter(kind=CreativeKind.AVATAR_TRAIT, is_active=True)
        return Response(
            AvatarLibrarySerializer(
                {
                    "avatars": _avatar_rows(workspace, rows),
                    "limits": {
                        **avatars.limits(workspace),
                        "draft_credits": avatars.draft_price(workspace),
                    },
                    "traits": [
                        {
                            "key": row.key,
                            "label": row.label,
                            "group": (row.metadata or {}).get("group", ""),
                            **(
                                {"values": row.metadata["values"]}
                                if (row.metadata or {}).get("values")
                                else {}
                            ),
                        }
                        for row in traits
                    ],
                }
            ).data
        )

    @extend_schema(
        request=AvatarCreateSerializer,
        responses={201: AvatarSerializer},
        summary="Save a new avatar",
        description=(
            "Invented: from a ready draft's portrait; 409 `avatar_draft_not_ready`. "
            "Real: admin only (403), 402 `feature_not_available` below the plan, "
            "409 `avatar_consent_required` without a signed consent, "
            "400 `consent_signer` when the signer is not the person. Either: "
            "402 `avatar_limit_reached` at `max_avatars`; 404 for another brand's draft or photo."
        ),
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        payload = AvatarCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        user = authenticated_user(request)
        if data["kind"] == "real":
            if Permission.ADMIN not in caller_permissions(request):
                raise PermissionDenied("Recording a person's consent needs the admin permission.")
            avatar = avatars.create_real(
                workspace,
                user=user,
                name=data["name"],
                photos=data["photos"],
                signed_by=data["signed_by"],
                signed_on=data["signed_on"],
                languages=data["languages"],
            )
        else:
            if data["draft"] is None:
                raise serializers.ValidationError({"draft": "Pick a portrait from a draft."})
            avatar = avatars.create_synthetic(
                workspace,
                user=user,
                draft_id=data["draft"],
                index=data["index"],
                name=data["name"],
                languages=data["languages"],
                is_house=data["is_house"],
            )
        return Response(_one(workspace, avatar), status=status.HTTP_201_CREATED)


class AvatarLimitsView(APIView):
    permission_classes = _READ

    @extend_schema(
        responses={200: AvatarLimitsSerializer},
        summary="The brand's avatar limits only: what Settings shows as used / max",
    )
    def get(self, request: Request) -> Response:
        workspace = request_workspace(request)
        return Response(
            AvatarLimitsSerializer(
                {**avatars.limits(workspace), "draft_credits": avatars.draft_price(workspace)}
            ).data
        )


class AvatarDraftListView(APIView):
    permission_classes: list[Any] = _WRITE

    @extend_schema(
        request=AvatarDraftRequestSerializer,
        responses={202: AvatarDraftSerializer},
        summary="Draw four portraits to pick a new avatar from",
        description=(
            "Queued on `ai_q`; poll `GET /brand/avatars/drafts/{id}/`. Credits are taken when "
            "the portraits are ready. 400 for an unknown trait; 402 `feature_not_available` "
            "when the plan has no avatars, `avatar_drafts_exhausted` past "
            "`avatar_drafts_monthly`, `insufficient_credits`."
        ),
    )
    def post(self, request: Request) -> Response:
        workspace = request_workspace(request)
        payload = AvatarDraftRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        draft = avatars.start_draft(
            workspace, user=authenticated_user(request), raw_traits=payload.validated_data["traits"]
        )
        return Response(_draft(workspace, draft), status=status.HTTP_202_ACCEPTED)


class AvatarDraftDetailView(APIView):
    permission_classes: list[Any] = _READ

    @extend_schema(responses={200: AvatarDraftSerializer}, summary="One portrait batch")
    def get(self, request: Request, pk: int) -> Response:
        workspace = request_workspace(request)
        draft = AvatarDraft.objects.filter(workspace=workspace, pk=pk).first()
        if draft is None:
            raise NotFound
        return Response(_draft(workspace, draft))


class _AvatarAction(APIView):
    permission_classes: list[Any] = _WRITE

    def act(self, request: Request, avatar: Avatar) -> Avatar:
        raise NotImplementedError

    @extend_schema(request=None, responses={200: AvatarSerializer})
    def post(self, request: Request, pk: int) -> Response:
        workspace = request_workspace(request)
        avatar = avatars.get(workspace, pk)
        return Response(_one(workspace, self.act(request, avatar)))


class AvatarHouseView(_AvatarAction):
    """Pin as the house face: first in every list, the Studio's default."""

    def act(self, request: Request, avatar: Avatar) -> Avatar:
        return avatars.make_house(avatar)


class AvatarArchiveView(_AvatarAction):
    """Remove from the library and from future renders; nothing is deleted."""

    def act(self, request: Request, avatar: Avatar) -> Avatar:
        return avatars.archive(avatar, actor=authenticated_user(request))


class AvatarRevokeView(_AvatarAction):
    """Withdraw a real person's consent (admin). Recorded in the audit log."""

    permission_classes: list[Any] = _ADMIN

    def act(self, request: Request, avatar: Avatar) -> Avatar:
        return avatars.revoke(avatar, actor=authenticated_user(request))
