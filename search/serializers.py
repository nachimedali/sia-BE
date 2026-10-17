"""Response shapes for `GET /search/`. Declared so the OpenAPI schema — and the
frontend types generated from it — describe the payload exactly."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers


class SearchHitSerializer(serializers.Serializer[Any]):
    type = serializers.ChoiceField(choices=["post", "product", "campaign"])
    id = serializers.IntegerField()
    title = serializers.CharField()
    #: The full caption or description; the client cuts the snippet.
    text = serializers.CharField(allow_blank=True)
    #: `PostStatus`, `CampaignStatus`, or `READY` / `NEEDS_REFERENCE` for a product.
    status = serializers.CharField()
    platforms = serializers.ListField(child=serializers.CharField())
    date = serializers.DateTimeField()
    #: What `date` is: `Edited`, `Scheduled`, `Published`, `Starts`, `Ends`, `Ended`.
    date_label = serializers.CharField()


class SearchCountsSerializer(serializers.Serializer[Any]):
    all = serializers.IntegerField()
    posts = serializers.IntegerField()
    products = serializers.IntegerField()
    campaigns = serializers.IntegerField()


class SearchResultsSerializer(serializers.Serializer[Any]):
    """Only the groups the request asked for are present: all three on `all`,
    one on a single type."""

    posts = SearchHitSerializer(many=True, required=False)
    products = SearchHitSerializer(many=True, required=False)
    campaigns = SearchHitSerializer(many=True, required=False)


class NextPlanSerializer(serializers.Serializer[Any]):
    name = serializers.CharField()
    days = serializers.IntegerField()


class SearchHistorySerializer(serializers.Serializer[Any]):
    plan = serializers.CharField()
    #: The plan's `analytics_history_days`: how far back posts and campaigns are searched.
    days = serializers.IntegerField()
    #: Matches older than `days`, counted and never returned.
    older_matches = serializers.IntegerField()
    next_plan = NextPlanSerializer(allow_null=True)


class SearchResponseSerializer(serializers.Serializer[Any]):
    q = serializers.CharField()
    type = serializers.ChoiceField(choices=["all", "posts", "products", "campaigns"])
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    counts = SearchCountsSerializer()
    results = SearchResultsSerializer()
    history = SearchHistorySerializer()
