"""Channel serialisation (design.md §7).

`SocialAccountSerializer` is read-only end to end: an account is created by
completing an OAuth flow, never by POSTing a body, so there is no writable
shape to expose. What *is* writable is the connect request; the cap (I6) is
checked by `services.complete_connect` before the adapter is called at all.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from channels import services
from channels.models import SocialAccount
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
            "provider_key",
            "reconnect_reason",
            "connected_at",
        )
        read_only_fields = fields


class AccountCardSerializer(serializers.Serializer[dict[str, Any]]):
    """One account as the connections page draws it (`channels.overview`).
    Every count that could be unknown is nullable — unknown is never 0."""

    account = SocialAccountSerializer()
    state = serializers.ChoiceField(
        choices=["connected", "syncing", "reconnect", "removed", "over", "disconnected"]
    )
    insights_off = serializers.BooleanField()
    followers = serializers.IntegerField(allow_null=True)
    followers_change_7d = serializers.IntegerField(allow_null=True)
    last_synced_at = serializers.DateTimeField(allow_null=True)
    scheduled = serializers.IntegerField()
    awaiting_approval = serializers.IntegerField()
    published_30d = serializers.IntegerField()


class CapShareSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField()
    count = serializers.IntegerField()
    current = serializers.BooleanField()


class CapSummarySerializer(serializers.Serializer[dict[str, Any]]):
    limit = serializers.IntegerField(allow_null=True)
    used = serializers.IntegerField()
    over = serializers.IntegerField()
    by_workspace = CapShareSerializer(many=True)


class ConnectionsOverviewSerializer(serializers.Serializer[dict[str, Any]]):
    cards = AccountCardSerializer(many=True)
    cap = CapSummarySerializer()
    can_manage = serializers.BooleanField()


class MetricShareSerializer(serializers.Serializer[dict[str, Any]]):
    available = serializers.BooleanField()
    delay_hours = serializers.IntegerField()
    note = serializers.CharField(allow_blank=True)


class PlatformShareSerializer(serializers.Serializer[dict[str, Any]]):
    metrics = serializers.DictField(child=MetricShareSerializer())
    comments = serializers.BooleanField()
    requires = serializers.CharField(allow_blank=True)
    connectable = serializers.BooleanField()
    unavailable_reason = serializers.CharField(allow_blank=True)
    selection = serializers.BooleanField()


class PlatformSharesSerializer(serializers.Serializer[dict[str, Any]]):
    provider = serializers.CharField()
    platforms = serializers.DictField(child=PlatformShareSerializer())


class ImpactPostSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    master_body = serializers.CharField()
    scheduled_at = serializers.DateTimeField(allow_null=True)


class DisconnectImpactSerializer(serializers.Serializer[dict[str, Any]]):
    only_here = ImpactPostSerializer(many=True)
    also_elsewhere = ImpactPostSerializer(many=True)


class DisconnectRequestSerializer(serializers.Serializer[dict[str, Any]]):
    scheduled = serializers.ChoiceField(
        choices=list(services.ScheduledFate.CHOICES), default=services.ScheduledFate.REMIND
    )


class DisconnectResultSerializer(serializers.Serializer[dict[str, Any]]):
    account = SocialAccountSerializer()
    reminded = serializers.IntegerField()
    unscheduled = serializers.IntegerField()
    kept_elsewhere = serializers.IntegerField()


class ConnectStartResponseSerializer(serializers.Serializer[dict[str, Any]]):
    auth_url = serializers.URLField()


class ConnectTargetSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.CharField()
    name = serializers.CharField()


class ConnectCompleteRequestSerializer(serializers.Serializer[dict[str, Any]]):
    #: Optional when `params.state` is ours: the signed state names the
    #: platform, so a provider that returns only its own query params (as
    #: SocialAPI does) still finishes on the right platform.
    platform = serializers.ChoiceField(choices=Platform.choices, required=False)
    #: The OAuth callback's query params on the first call, and
    #: `ConnectResolution.params` handed back verbatim on the second.
    params = serializers.DictField(default=dict)
    target_id = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        params = attrs.get("params") or {}
        if not attrs.get("platform"):
            platform = services.read_state(str(params.get("state") or "")).get("p")
            if platform not in Platform.values:
                raise serializers.ValidationError(
                    {"platform": "Say which platform this connection is for."}
                )
            attrs["platform"] = platform
        return attrs


class ConnectCompleteResponseSerializer(serializers.Serializer[dict[str, Any]]):
    """Two shapes in one: either the account is attached, or the user still
    has a page/organisation to pick. `needs_selection` is what the client
    branches on."""

    needs_selection = serializers.BooleanField()
    #: Which platform this connection is for — read from the signed state when
    #: the provider's return did not say, so the page can name it.
    platform = serializers.ChoiceField(choices=Platform.choices)
    account = SocialAccountSerializer(required=False, allow_null=True)
    targets = ConnectTargetSerializer(many=True, required=False)
    params = serializers.DictField(required=False)


class RemoteOptionSerializer(serializers.Serializer[Any]):
    """One choice in a provider-backed list — a Pinterest board (P4-02).

    `id` and `name` only. The provider's own row carries more, and forwarding
    it would put a vendor's shape in front of the composer, which is the thing
    the adapter layer exists to prevent.
    """

    id = serializers.CharField()
    name = serializers.CharField()


class RemoteOptionListSerializer(serializers.Serializer[Any]):
    options = RemoteOptionSerializer(many=True)
