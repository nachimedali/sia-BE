"""Trend serialisation (design.md §7, §10.5, D18).

Two shapes, and the difference between them is the gate. A paid workspace gets
`TrendClusterSerializer` — the cluster and its exemplars, enough to act on. Free
gets `TrendTeaserSerializer`: the same clusters, capped in number, with the
exemplars withheld and `locked: true` set, so the UI renders the upgrade prompt
from data rather than from its own idea of what a plan includes (§10.5).

Withholding the exemplars is the substantive half. The label and the evidence
count tell a Free user the corpus is real; the exemplars are the part worth
paying for, so they never leave the server for a workspace without
`trend_engine`.

Both method fields are `@extend_schema_field`-annotated. An un-annotated
`SerializerMethodField` types as `unknown` in the generated client and forces
the frontend to assert its way around it — the schema-accuracy gap Phase 8
found the hard way and fixed twice.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from trends.models import TrendCluster, TrendItem

#: What Free sees before the upgrade prompt (D18: "a few cards").
TEASER_LIMIT = 3

#: Exemplars per cluster. Enough to show the pattern is real, few enough that
#: the response stays one screen.
EXEMPLAR_LIMIT = 3


class TrendExemplarSerializer(serializers.ModelSerializer[TrendItem]):
    class Meta:
        model = TrendItem
        fields = ("id", "modality", "author_handle", "body", "media_url", "posted_at")
        read_only_fields = fields


class TrendClusterSerializer(serializers.ModelSerializer[TrendCluster]):
    exemplars = serializers.SerializerMethodField()
    locked = serializers.SerializerMethodField()

    class Meta:
        model = TrendCluster
        fields = (
            "id",
            "label",
            "platform",
            "item_count",
            "velocity_score",
            "recency_score",
            "composite_score",
            "window_start",
            "window_end",
            "expires_at",
            "exemplars",
            "locked",
        )
        read_only_fields = fields

    @extend_schema_field(TrendExemplarSerializer(many=True))
    def get_exemplars(self, cluster: TrendCluster) -> list[dict[str, Any]]:
        # No `sorted()`: `TrendItem.Meta.ordering` is `(-composite_score,
        # -posted_at)`, so `cluster.items.all()` is already in this order —
        # from the view's prefetch when there is one, from the DB's own
        # default ordering when there isn't.
        items = list(cluster.items.all())[:EXEMPLAR_LIMIT]
        return list(TrendExemplarSerializer(items, many=True).data)

    @extend_schema_field(serializers.BooleanField())
    def get_locked(self, cluster: TrendCluster) -> bool:
        return False


class TrendTeaserSerializer(TrendClusterSerializer):
    """D18: "a few cards." The cap lives here, not in the caller, so the whole
    shape of a teaser — capped, exemplar-free, locked — is one class to read
    rather than split between this file and whoever constructs it."""

    @classmethod
    def many_init(cls, *args: Any, **kwargs: Any) -> Any:
        if args:
            args = (list(args[0])[:TEASER_LIMIT], *args[1:])
        return super().many_init(*args, **kwargs)

    @extend_schema_field(TrendExemplarSerializer(many=True))
    def get_exemplars(self, cluster: TrendCluster) -> list[dict[str, Any]]:
        return []

    @extend_schema_field(serializers.BooleanField())
    def get_locked(self, cluster: TrendCluster) -> bool:
        return True


class TrendRefreshRequestSerializer(serializers.Serializer[dict[str, Any]]):
    platform = serializers.CharField()
