"""Commercial numbers live in the database, never in the environment (I8,
Part 7 rule 10).

The split this file defends:

* **`.env` is wiring** — where things are, which vendor answers, which secret
  opens the door;
* **admin is commerce** — prices, quotas, caps, trials and the feature map.

Crossing it is cheap to do and expensive to discover. A price in an environment
variable means changing it needs a deploy, and two environments can quietly
disagree about what a customer bought — with the ledger, the invoice and the
entitlement resolver all citing a different number.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest
from django.conf import settings
from django.contrib import admin

from billing.models import FEATURE_KEYS, QUOTA_FIELDS, Pack, Plan

pytestmark = pytest.mark.django_db

SETTINGS_DIR = pathlib.Path(settings.BASE_DIR) / "config" / "settings"

#: Words that name money or an allowance. A settings variable containing one of
#: these is almost certainly a commercial number that belongs on a row.
COMMERCIAL_WORDS = ("PRICE", "QUOTA", "CREDIT", "ALLOWANCE", "MAX_", "_LIMIT", "TIER")

#: The false positives, named so an exemption is a deliberate edit rather than
#: a loosened regex. Each is infrastructure that happens to share a word.
ALLOWED: frozenset[str] = frozenset(
    {
        # Provider rate limits — vendor capacity, not anything a customer buys.
        "RATELIMIT_REDIS_URL",
        # Postgres connection reuse, in seconds. "MAX_AGE" trips the word list
        # and has nothing to do with commerce.
        "DB_CONN_MAX_AGE",
    }
)


def _declared_env_names() -> set[str]:
    names: set[str] = set()
    for module in SETTINGS_DIR.glob("*.py"):
        names |= set(re.findall(r'env(?:\.\w+)?\(\s*"([A-Z_0-9]+)"', module.read_text()))
    return names


# -----------------------------------------------------------------------------
# The boundary
# -----------------------------------------------------------------------------
def test_no_commercial_number_is_read_from_the_environment() -> None:
    offenders = sorted(
        name
        for name in _declared_env_names()
        if name not in ALLOWED and any(word in name for word in COMMERCIAL_WORDS)
    )

    assert offenders == [], (
        f"These settings look commercial: {', '.join(offenders)}. Prices, quotas and "
        "caps belong on a Plan or Pack row where an operator edits them without a "
        "deploy (Part 7 rule 10). Add to ALLOWED only if the name is infrastructure."
    )


def test_the_env_template_says_so_in_writing() -> None:
    """The rule is only useful if the next person reading `.env.example`
    encounters it before adding a variable, not after."""
    template = (pathlib.Path(settings.BASE_DIR) / ".env.example").read_text()

    assert "No price. No quota. No limit." in template
    assert "seed_plans" in template


def test_the_env_template_covers_every_variable_the_settings_read() -> None:
    """A variable the app reads and the template omits is a variable somebody
    discovers by way of a 500 in an environment they cannot reproduce."""
    template = (pathlib.Path(settings.BASE_DIR) / ".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z_0-9]+)=", template, re.M))

    missing = sorted(_declared_env_names() - documented)

    assert missing == [], f"Undocumented in .env.example: {', '.join(missing)}"


def test_the_local_env_matches_the_template_shape() -> None:
    """Same variables, so a missing one shows up as a diff rather than as a
    runtime error nobody can reproduce."""
    base = pathlib.Path(settings.BASE_DIR)
    template = set(re.findall(r"^#?\s*([A-Z_0-9]+)=", (base / ".env.example").read_text(), re.M))
    local = set(re.findall(r"^#?\s*([A-Z_0-9]+)=", (base / ".env").read_text(), re.M))

    assert template - local == set(), f"Missing from .env: {sorted(template - local)}"


# -----------------------------------------------------------------------------
# …and every commercial number really is editable
# -----------------------------------------------------------------------------
def _admin_fields(model: Any) -> set[str]:
    """Every field name the admin form actually exposes.

    Flattens the nested groups Django allows inside `fields` — a tuple in there
    puts two inputs on one row, and a reader of this set should not have to
    care about layout.
    """
    site = admin.site._registry[model]
    declared: set[str] = set()
    for _label, options in site.fieldsets or ():
        for entry in options["fields"]:
            declared |= {entry} if isinstance(entry, str) else set(entry)
    return declared


def test_every_plan_quota_is_editable_in_admin() -> None:
    """`QUOTA_FIELDS` is what the entitlement resolver may be asked for. Any one
    of them missing from the form is a number nobody can change without a
    deploy, which is the failure this whole rule exists to prevent."""
    missing = sorted(QUOTA_FIELDS - _admin_fields(Plan))

    assert missing == [], f"Not editable in the Plan admin: {', '.join(missing)}"


def test_every_price_field_is_editable_in_admin() -> None:
    prices = {
        "price_monthly_cents",
        "price_annual_cents",
        "price_per_workspace_cents",
    }

    assert prices <= _admin_fields(Plan)


def test_the_feature_map_is_editable_in_admin() -> None:
    assert "features" in _admin_fields(Plan)


def test_the_admin_lists_the_valid_feature_keys() -> None:
    """A JSON textarea with no key list is a typo generator. The model rejects
    unknown keys, but an operator should not have to discover the vocabulary by
    triggering a validation error."""
    site = admin.site._registry[Plan]
    descriptions = " ".join(
        str(options.get("description", "")) for _label, options in site.fieldsets or ()
    )

    for key in FEATURE_KEYS:
        assert key in descriptions, f"{key} is not documented on the Plan admin form"


def test_no_plan_field_is_silently_unreachable() -> None:
    """Declaring `fieldsets` is what makes the form navigable, and also what can
    accidentally hide a field: anything omitted simply stops being editable.
    This catches the omission at the moment a column is added."""
    editable = {
        field.name
        for field in Plan._meta.get_fields()
        if getattr(field, "editable", False) and not field.auto_created
    }

    assert editable - _admin_fields(Plan) == set(), (
        f"Unreachable in the Plan admin: {sorted(editable - _admin_fields(Plan))}"
    )


def test_pack_price_and_size_stay_editable() -> None:
    """`Pack` has no fieldsets, so every editable field is on the form by
    default. Asserted anyway, because adding fieldsets later is exactly when
    one would get dropped."""
    from django.test import RequestFactory

    site = admin.site._registry[Pack]
    request = RequestFactory().get("/admin/")

    assert site.fieldsets is None
    assert site.get_form(request).base_fields.keys() >= {"units", "price_cents"}
