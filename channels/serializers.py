"""Channel serialisation (design.md §7).

`SocialAccountSerializer` is read-only end to end: an account is created by
completing an OAuth flow, never by POSTing a body, so there is no writable
shape to expose. What *is* writable is the connect request — and
`ConnectCompleteRequestSerializer` is the first of I6's three layers, refusing
a workspace already at its cap before the adapter is called at all.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from channels import services
from channels.models import SocialAccount
from common.workspaces import active_workspace
from content.models import Platform


class SocialAccountSerializer(serializers.ModelSerializer[SocialAccount]):
    class Meta:
        model = SocialAccount
        fields = (
            "id",
            "platform",
            "handle",
            "display_name",
            "followers_cached",
            "status",
            "connected_at",
        )
        read_only_fields = fields


class ConnectStartResponseSerializer(serializers.Serializer[dict[str, Any]]):
    auth_url = serializers.URLField()


class ConnectTargetSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.CharField()
    name = serializers.CharField()


class ConnectCompleteRequestSerializer(serializers.Serializer[dict[str, Any]]):
    platform = serializers.ChoiceField(choices=Platform.choices)
    #: The OAuth callback's query params on the first call, and
    #: `ConnectResolution.params` handed back verbatim on the second.
    params = serializers.DictField(default=dict)
    target_id = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # I6, layer one. Raising here rather than in `create` keeps the check
        # ahead of any provider call — the workspace is told it is at its cap
        # before OCCS spends a request finding out.
        request = self.context.get("request")
        if request is not None:
            services.enforce_account_cap(active_workspace(request))
        return attrs


class ConnectCompleteResponseSerializer(serializers.Serializer[dict[str, Any]]):
    """Two shapes in one: either the account is attached, or the user still
    has a page/organisation to pick. `needs_selection` is what the client
    branches on."""

    needs_selection = serializers.BooleanField()
    account = SocialAccountSerializer(required=False, allow_null=True)
    targets = ConnectTargetSerializer(many=True, required=False)
    params = serializers.DictField(required=False)
