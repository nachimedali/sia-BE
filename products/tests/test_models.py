from __future__ import annotations

from typing import Any

import pytest

from products.models import Product

pytestmark = pytest.mark.django_db


def test_str_returns_name(product: Any) -> None:
    assert str(product) == "Aurora Ceramic Mug"


def test_create_product_defaults_to_not_generation_ready(product: Any) -> None:
    assert product.is_generation_ready is False


def test_product_is_workspace_scoped(workspace: Any, product: Any) -> None:
    assert list(Product.objects.filter(workspace=workspace)) == [product]
