from rest_framework import serializers

from categories.models import Category


class CategorySerializer(serializers.ModelSerializer[Category]):
    class Meta:
        model = Category
        fields = ("id", "name", "slug", "parent")
        read_only_fields = fields
