"""The generated API types are imported by name on the frontend, so a name
spectacular invents for a collision ("RoleD9cEnum", "..._retrieve_2") is a
contract that moves whenever any other choice set or route moves."""

from __future__ import annotations

import re

import pytest
from drf_spectacular.generators import SchemaGenerator

pytestmark = pytest.mark.django_db


def test_no_schema_name_is_a_collision_fallback() -> None:
    schema = SchemaGenerator().get_schema(request=None, public=True)

    hash_suffix = re.compile(r"[a-z][0-9A-F][0-9a-f]{2}Enum$")
    hashed = [n for n in schema["components"]["schemas"] if hash_suffix.search(n)]
    numbered = [
        op["operationId"]
        for path in schema["paths"].values()
        for op in path.values()
        if re.search(r"_\d+$", op["operationId"])
    ]

    assert hashed == [], "name it in SPECTACULAR_SETTINGS['ENUM_NAME_OVERRIDES']"
    assert numbered == [], "give the view an explicit operation_id"
