"""Analytics serialisation (design.md §7).

Mostly plain `Serializer`s over dataclasses rather than `ModelSerializer`s: what
the endpoints return is `analytics.services.signals`' derived view of the data,
not the capture rows themselves. A client that wanted raw captures would be
reading the pipeline's working state.

Every `SerializerMethodField` here is `@extend_schema_field`-annotated for the
same reason as elsewhere — an un-annotated one types as `unknown` in the
generated client.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from analytics.models import Comment, RepurposeCandidate


class TargetPerformanceSerializer(serializers.Serializer[Any]):
    target_id = serializers.IntegerField()
    post_id = serializers.IntegerField()
    platform = serializers.CharField()
    published_at = serializers.DateTimeField()
    engagement_rate = serializers.FloatField()
    impressions = serializers.IntegerField()
    likes = serializers.IntegerField()
    comments = serializers.IntegerField()
    #: Null until the account has enough history to rank against — the UI shows
    #: "not enough history yet" rather than a number that looks authoritative.
    percentile = serializers.FloatField(allow_null=True)
    decay_ratio = serializers.FloatField()


class AttributionBucketSerializer(serializers.Serializer[Any]):
    posts = serializers.IntegerField()
    engagement_rate = serializers.FloatField()


class BestTimeSerializer(serializers.Serializer[Any]):
    weekday = serializers.IntegerField(help_text="0 = Monday, 6 = Sunday.")
    hour = serializers.IntegerField()
    samples = serializers.IntegerField()
    engagement_rate = serializers.FloatField()


class OverviewSerializer(serializers.Serializer[Any]):
    posts = serializers.IntegerField()
    impressions = serializers.IntegerField()
    engagement_rate = serializers.FloatField()
    top = TargetPerformanceSerializer(many=True)
    #: Keyed by `PostSource` (`AI`/`MANUAL`) — §8.9's honest self-audit.
    by_source = serializers.DictField(child=AttributionBucketSerializer())
    #: Keyed by media kind (`IMAGE`/`VIDEO`/`TEXT`).
    by_media = serializers.DictField(child=AttributionBucketSerializer())
    #: Included here rather than left to the standalone endpoint: best times are
    #: a pure function of the same scan, so fetching them separately meant the
    #: page ran that scan twice.
    best_times = BestTimeSerializer(many=True)


class SentimentSummarySerializer(serializers.Serializer[Any]):
    total = serializers.IntegerField()
    positive = serializers.IntegerField()
    neutral = serializers.IntegerField()
    negative = serializers.IntegerField()
    #: Mean of the per-comment scores, -1 to 1.
    score = serializers.FloatField()


class CommentSerializer(serializers.ModelSerializer[Comment]):
    class Meta:
        model = Comment
        fields = (
            "id",
            "post_target",
            "author",
            "body",
            "sentiment",
            "sentiment_score",
            "posted_at",
        )
        read_only_fields = fields


class RepurposeCandidateSerializer(serializers.ModelSerializer[RepurposeCandidate]):
    post_body = serializers.CharField(source="post.master_body", read_only=True)

    class Meta:
        model = RepurposeCandidate
        fields = (
            "id",
            "post",
            "post_body",
            "published_at",
            "percentile",
            "reason",
            "score",
            "surfaced_at",
        )
        read_only_fields = fields
