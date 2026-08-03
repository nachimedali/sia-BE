"""Category tree and seed (design.md §6.1, D11)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from rest_framework.test import APIClient

from categories.models import Category

pytestmark = pytest.mark.django_db

User = get_user_model()
CATEGORIES_URL = "/api/v1/categories/"


@pytest.fixture
def seeded():
    call_command("seed_categories", verbosity=0)


@pytest.fixture
def client():
    api = APIClient()
    api.force_authenticate(User.objects.create_user(email="a@example.com", password="x"))
    return api


def test_seed_covers_the_wizard_choices(seeded) -> None:
    """The onboarding chips have to resolve to real rows."""
    for name in (
        "Home & Lifestyle",
        "Fashion & Apparel",
        "Food & Beverage",
        "Beauty & Wellness",
        "Sports & Outdoors",
        "Education",
    ):
        assert Category.objects.filter(name=name, parent__isnull=True).exists()


def test_seed_covers_verticals_beyond_the_wizard(seeded) -> None:
    """D11: extraction is on demand per category, so breadth costs nothing
    until a workspace in that vertical asks for a Playbook."""
    assert Category.objects.filter(parent__isnull=True).count() >= 12


def test_seed_is_idempotent(seeded) -> None:
    before = Category.objects.count()
    call_command("seed_categories", verbosity=0)
    assert Category.objects.count() == before


def test_children_are_attached_to_their_root(seeded) -> None:
    skincare = Category.objects.get(name="Skincare")
    assert skincare.parent is not None
    assert skincare.parent.name == "Beauty & Wellness"
    assert [c.name for c in skincare.ancestors()] == ["Beauty & Wellness"]


def test_child_slugs_are_namespaced_by_their_root(seeded) -> None:
    """Child slugs embed the root, so the same child name can appear under two
    verticals without colliding — "Fragrance" under both Beauty and Home, say."""
    assert Category.objects.get(name="Skincare").slug == "beauty-wellness-skincare"

    beauty = Category.objects.get(name="Beauty & Wellness")
    home = Category.objects.get(name="Home & Lifestyle")
    Category.objects.create(name="Soap", slug="beauty-wellness-soap", parent=beauty)
    Category.objects.create(name="Soap", slug="home-lifestyle-soap", parent=home)

    assert Category.objects.filter(name="Soap").count() == 2


def test_all_slugs_are_unique(seeded) -> None:
    slugs = list(Category.objects.values_list("slug", flat=True))
    assert len(slugs) == len(set(slugs))


def test_list_endpoint_returns_the_whole_tree_unpaginated(seeded, client) -> None:
    body = client.get(CATEGORIES_URL).json()
    # The wizard renders the tree in one go; paging it would be a worse API.
    assert isinstance(body, list)
    assert len(body) == Category.objects.count()


def test_roots_only_filter(seeded, client) -> None:
    body = client.get(f"{CATEGORIES_URL}?roots_only=true").json()
    assert all(row["parent"] is None for row in body)


def test_children_filter(seeded, client) -> None:
    beauty = Category.objects.get(name="Beauty & Wellness")
    body = client.get(f"{CATEGORIES_URL}?parent={beauty.id}").json()
    assert {row["name"] for row in body} >= {"Skincare", "Haircare"}


def test_inactive_categories_are_hidden(seeded, client) -> None:
    Category.objects.filter(name="Skincare").update(is_active=False)
    names = {row["name"] for row in client.get(CATEGORIES_URL).json()}
    assert "Skincare" not in names


def test_categories_require_authentication(seeded) -> None:
    assert APIClient().get(CATEGORIES_URL).status_code == 401
