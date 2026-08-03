"""OpenAPI schema (design.md §7).

The frontend client is generated from this document, so a schema that fails to
render breaks the FE build rather than merely losing documentation.
"""

from __future__ import annotations

import pytest
import yaml
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

SCHEMA_URL = "/api/v1/schema/"


@pytest.fixture
def schema() -> dict:
    response = APIClient().get(SCHEMA_URL)
    assert response.status_code == 200
    return yaml.safe_load(response.content)


def test_schema_renders(schema: dict) -> None:
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == "OCCS API"


def test_every_phase_1_endpoint_is_documented(schema: dict) -> None:
    for path in ("/api/v1/health/", "/api/v1/auth/login/", "/api/v1/auth/refresh/"):
        assert path in schema["paths"], f"{path} is missing from the schema"


def test_schema_is_reachable_without_authentication() -> None:
    assert APIClient().get(SCHEMA_URL).status_code == 200
