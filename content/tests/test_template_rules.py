"""S18 — product templates on a rhythm (`todo-steps.md` S18).

A template is an offer run on a rule ("Happy hour -20%, every Friday at
09:00") for chosen products and, when connected, chosen accounts. Each slot
makes **one draft per product**, already carrying its slot time and submitted
for review: a person approves or edits it (L-2), and approving it schedules it
at the slot. Generating text or an image spends credits and never writes to the
product. A slot the balance cannot pay for makes no draft and says so.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import time_machine
from django.contrib.auth import get_user_model

from billing.models import FeatureFlag
from billing.services import ledger
from billing.services.flags import TEMPLATE_RULES_S18
from content.models import (
    DeliveryMode,
    PostStatus,
    PostTemplate,
    RecurrenceOccurrence,
    RecurrenceRule,
)
from content.services import recurrence
from content.tasks import recurrence_warn
from notifications.models import EventKey, Notification
from products.models import Product
from scheduling.publishing import build_targets
from workspaces.services import approvals
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

TEMPLATES_URL = "/api/v1/post-templates/"
RULES_URL = "/api/v1/recurrence-rules/"
OPTIONS_URL = "/api/v1/post-templates/options/"
PARIS = ZoneInfo("Europe/Paris")
#: A Wednesday. The Fridays inside a 10-day horizon are 6 and 13 March.
NOW = "2026-03-04 12:00:00+00:00"
FRIDAY_NINE = "FREQ=WEEKLY;BYDAY=FR;BYHOUR=9;BYMINUTE=0;BYSECOND=0"


@pytest.fixture(autouse=True)
def _paid(paid_workspace: Any) -> None:
    """Refrains are off on Spark, the default plan."""


@pytest.fixture
def products(workspace: Any, media_asset: Any) -> list[Any]:
    tea = Product.objects.create(
        workspace=workspace,
        name="Mint tea",
        price=Decimal("4.500"),
        price_currency="TND",
        product_url="https://example.com/tea",
    )
    tea.reference_images.add(media_asset)
    cake = Product.objects.create(workspace=workspace, name="Orange cake", price=Decimal("12"))
    return [tea, cake]


def _template(workspace: Any, user: Any, **payload: Any) -> Any:
    return PostTemplate.objects.create(
        workspace=workspace,
        name="Happy hour",
        created_by=user,
        payload={"master_body": "Happy hour: {product} -20%", **payload},
    )


def _rule(template: Any, **fields: Any) -> Any:
    return RecurrenceRule.objects.create(
        source=template,
        rrule=FRIDAY_NINE,
        timezone="Europe/Paris",
        **{"horizon_days": 10, **fields},
    )


def _made(**filters: Any) -> list[Any]:
    """The occurrences, oldest slot first, typed loosely: a made one has a post."""
    return list(
        RecurrenceOccurrence.objects.filter(**filters)
        .select_related("post", "product")
        .order_by("slot_at", "pk")
    )


def _credit(workspace: Any, user: Any, amount: int) -> None:
    ledger.adjust_credits(workspace, amount, actor=user, note="test")


# -----------------------------------------------------------------------------
# One draft per product, at the slot, waiting for a person
# -----------------------------------------------------------------------------
@time_machine.travel(NOW, tick=False)
def test_each_slot_makes_one_draft_per_product(workspace: Any, user: Any, products: Any) -> None:
    rule = _rule(_template(workspace, user, product_ids=[p.pk for p in products]))

    created = recurrence.materialise_due()

    slots = recurrence.planned_slots(rule)
    assert len(slots) == 2
    assert created == 4
    made = _made(rule=rule)
    assert {(o.slot_at, o.product_id) for o in made} == {
        (slot, p.pk) for slot in slots for p in products
    }
    assert all(o.post.product_id == o.product_id for o in made)


@time_machine.travel(NOW, tick=False)
def test_a_draft_carries_its_slot_and_waits_for_review(
    workspace: Any, user: Any, products: Any
) -> None:
    rule = _rule(_template(workspace, user, product_ids=[products[0].pk]))
    recurrence.materialise_due()

    for occurrence in _made(rule=rule):
        post = occurrence.post
        assert post.status == PostStatus.PENDING_REVIEW
        assert post.proposed_scheduled_at == occurrence.slot_at
        assert post.scheduled_at is None, "nothing is scheduled before a person approves"
        assert occurrence.slot_at.astimezone(PARIS).hour == 9


@time_machine.travel(NOW, tick=False)
def test_placeholders_are_filled_from_each_product(
    workspace: Any, user: Any, products: Any
) -> None:
    template = _template(workspace, user, product_ids=[p.pk for p in products])
    template.payload["master_body"] = "{product} at {price} — {link}"
    template.save(update_fields=["payload"])
    _rule(template)
    recurrence.materialise_due()

    bodies = {o.product.name: o.post.master_body for o in _made()}
    assert bodies["Mint tea"] == "Mint tea at 4.500 TND — https://example.com/tea"
    assert bodies["Orange cake"] == "Orange cake at 12.000 TND — "


@time_machine.travel(NOW, tick=False)
def test_the_product_photo_is_the_image_by_default(
    workspace: Any, user: Any, products: Any, media_asset: Any
) -> None:
    _rule(_template(workspace, user, product_ids=[p.pk for p in products]))
    recurrence.materialise_due()

    tea = _made(product=products[0])[0]
    cake = _made(product=products[1])[0]
    assert [a.media_asset_id for a in tea.post.media_attachments.all()] == [media_asset.pk]
    assert not cake.post.media_attachments.exists(), "no photo is no image, not an error"


@time_machine.travel(NOW, tick=False)
def test_a_second_scan_creates_nothing(workspace: Any, user: Any, products: Any) -> None:
    _rule(_template(workspace, user, product_ids=[p.pk for p in products]))
    assert recurrence.materialise_due() == 4
    assert recurrence.materialise_due() == 0


@time_machine.travel(NOW, tick=False)
def test_approving_a_draft_schedules_it_at_its_slot(
    workspace: Any, user: Any, products: Any
) -> None:
    _rule(_template(workspace, user, product_ids=[products[0].pk]))
    recurrence.materialise_due()
    occurrence = _made()[0]

    post = approvals.approve(occurrence.post, actor=user)

    assert post.scheduled_at == occurrence.slot_at
    assert post.status in {PostStatus.SCHEDULED, PostStatus.REMINDER_ARMED}


@time_machine.travel(NOW, tick=False)
def test_with_no_account_the_draft_becomes_a_reminder(
    workspace: Any, user: Any, products: Any
) -> None:
    _rule(_template(workspace, user, product_ids=[products[0].pk]))
    recurrence.materialise_due()

    post = _made()[0].post
    assert post.proposed_delivery_mode == DeliveryMode.REMINDER


@time_machine.travel(NOW, tick=False)
def test_chosen_accounts_auto_publish_and_only_to_them(
    workspace: Any, user: Any, products: Any, social_account: Any
) -> None:
    from channels.models import SocialAccount

    other = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        handle="acme",
        display_name="Acme Page",
        provider_account_id="acct-facebook-1",
    )
    _rule(
        _template(
            workspace, user, product_ids=[products[0].pk], social_account_ids=[social_account.pk]
        )
    )
    recurrence.materialise_due()
    post = _made()[0].post

    assert post.proposed_delivery_mode == DeliveryMode.AUTO_PUBLISH
    targets = build_targets(post)
    assert [t.social_account_id for t in targets] == [social_account.pk]
    assert other.pk not in post.planned_accounts


@time_machine.travel(NOW, tick=False)
def test_generating_never_writes_the_product(
    workspace: Any, user: Any, products: Any, generation_port: Any
) -> None:
    generation_port.body = "A fresh line about mint tea"
    _credit(workspace, user, 100)
    before = Product.objects.filter(pk=products[0].pk).values().get()
    _rule(_template(workspace, user, product_ids=[products[0].pk], text_mode="generate"))

    recurrence.materialise_due()

    assert Product.objects.filter(pk=products[0].pk).values().get() == before
    post = _made()[0].post
    assert post.master_body == "A fresh line about mint tea"
    assert generation_port.calls[0]["kind"] == "TEXT"
    assert "Happy hour: Mint tea -20%" in generation_port.calls[0]["prompt"]


@time_machine.travel(NOW, tick=False)
def test_a_failed_text_generation_falls_back_to_the_placeholder_text(
    workspace: Any, user: Any, products: Any, generation_port: Any
) -> None:
    generation_port.status = "FAILED"
    _credit(workspace, user, 100)
    _rule(_template(workspace, user, product_ids=[products[0].pk], text_mode="generate"))

    recurrence.materialise_due()

    assert _made()[0].post.master_body == "Happy hour: Mint tea -20%"


@time_machine.travel(NOW, tick=False)
def test_a_generated_image_replaces_the_product_photo(
    workspace: Any, user: Any, products: Any, generation_port: Any, make_png_upload: Any
) -> None:
    from content.services.media import ingest_media

    picture = ingest_media(workspace=workspace, upload=make_png_upload())
    generation_port.media_asset = picture
    _credit(workspace, user, 100)
    _rule(_template(workspace, user, product_ids=[products[0].pk], image_mode="generate"))

    recurrence.materialise_due()

    post = _made()[0].post
    assert [a.media_asset_id for a in post.media_attachments.all()] == [picture.pk]
    assert generation_port.calls[0]["kind"] == "IMAGE"


# -----------------------------------------------------------------------------
# A slot the balance cannot pay for
# -----------------------------------------------------------------------------
@time_machine.travel(NOW, tick=False)
def test_a_short_balance_skips_the_slot_and_says_so(
    workspace: Any, user: Any, products: Any, generation_port: Any
) -> None:
    _rule(_template(workspace, user, product_ids=[products[0].pk], text_mode="generate"))

    assert recurrence.materialise_due() == 0

    skipped = RecurrenceOccurrence.objects.all()
    assert skipped.count() == 2
    assert all(o.post_id is None and o.skipped_reason == "credits" for o in skipped)
    assert generation_port.calls == [], "nothing is generated it cannot pay for"
    notices = Notification.objects.filter(event_key=EventKey.TEMPLATE_SLOT_SKIPPED)
    assert notices.count() == 2

    recurrence.materialise_due()
    assert notices.count() == 2, "told once per slot, not once per scan"


@time_machine.travel(NOW, tick=False)
def test_a_skipped_slot_is_made_once_the_balance_is_topped_up(
    workspace: Any, user: Any, products: Any, generation_port: Any
) -> None:
    _rule(_template(workspace, user, product_ids=[products[0].pk], text_mode="generate"))
    recurrence.materialise_due()

    _credit(workspace, user, 100)
    assert recurrence.materialise_due() == 2
    assert all(o.post_id and not o.skipped_reason for o in RecurrenceOccurrence.objects.all())


# -----------------------------------------------------------------------------
# From / Until
# -----------------------------------------------------------------------------
@time_machine.travel(NOW, tick=False)
def test_slots_stay_inside_from_and_until(workspace: Any, user: Any, products: Any) -> None:
    rule = _rule(
        _template(workspace, user, product_ids=[products[0].pk]),
        horizon_days=30,
        starts_on=dt.date(2026, 3, 10),
        ends_on=dt.date(2026, 3, 20),
    )

    days = [slot.astimezone(PARIS).date() for slot in recurrence.planned_slots(rule)]
    assert days == [dt.date(2026, 3, 13), dt.date(2026, 3, 20)]


def test_until_before_from_is_400(auth_client: Any, workspace: Any, user: Any) -> None:
    template = _template(workspace, user)
    response = auth_client.post(
        RULES_URL,
        {
            "source": template.pk,
            "rrule": FRIDAY_NINE,
            "timezone": "Europe/Paris",
            "horizon_days": 30,
            "starts_on": "2026-03-20",
            "ends_on": "2026-03-10",
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_recurrence"


# -----------------------------------------------------------------------------
# Saving a template
# -----------------------------------------------------------------------------
def _save(auth_client: Any, **payload: Any) -> Any:
    return auth_client.post(
        TEMPLATES_URL,
        {"name": "Happy hour", "payload": {"master_body": "Happy hour", **payload}},
        format="json",
    )


def test_a_template_saves_its_products_accounts_and_modes(
    auth_client: Any, products: Any, social_account: Any
) -> None:
    response = _save(
        auth_client,
        master_body="{product} -20%",
        product_ids=[products[0].pk],
        social_account_ids=[social_account.pk],
        image_mode="generate",
        text_mode="generate",
    )
    assert response.status_code == 201
    payload = response.json()["payload"]
    assert payload["product_ids"] == [products[0].pk]
    assert payload["social_account_ids"] == [social_account.pk]
    assert (payload["image_mode"], payload["text_mode"]) == ("generate", "generate")


def test_an_unknown_placeholder_is_400(auth_client: Any, products: Any) -> None:
    response = _save(auth_client, master_body="{prodcut} -20%", product_ids=[products[0].pk])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_placeholder"


def test_a_placeholder_without_a_product_is_400(auth_client: Any) -> None:
    response = _save(auth_client, master_body="{product} -20%")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "placeholder_needs_product"


@pytest.mark.parametrize("field", ["image_mode", "text_mode"])
def test_an_unknown_mode_is_400(auth_client: Any, field: str) -> None:
    response = _save(auth_client, **{field: "sometimes"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_template_rules"


def _stranger_workspace(email: str) -> Any:
    stranger = get_user_model().objects.create_user(email=email, password="x")
    return provision_workspace(stranger, name="Someone Else")


@pytest.mark.parametrize("field", ["product_ids", "social_account_ids"])
def test_another_organizations_product_or_account_is_refused(auth_client: Any, field: str) -> None:
    from channels.models import SocialAccount

    theirs = _stranger_workspace("outside@example.com")
    foreign = (
        Product.objects.create(workspace=theirs, name="Not yours")
        if field == "product_ids"
        else SocialAccount.objects.create(
            workspace=theirs, platform="instagram", handle="@x", provider_account_id="x-1"
        )
    )
    response = _save(auth_client, **{field: [foreign.pk]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_template_rules"


def test_another_workspaces_product_is_refused(auth_client: Any, workspace: Any, user: Any) -> None:
    from workspaces.services.provisioning import provision_extra_workspace

    sibling = provision_extra_workspace(user=user, name="Sibling")
    assert sibling.organization == workspace.organization
    foreign = Product.objects.create(workspace=sibling, name="Sibling's")
    response = _save(auth_client, product_ids=[foreign.pk])
    assert response.status_code == 400


def test_another_workspaces_template_rules_are_404(auth_client: Any) -> None:
    theirs = _stranger_workspace("stranger@example.com")
    foreign = PostTemplate.objects.create(workspace=theirs, name="Not yours")
    assert auth_client.patch(f"{TEMPLATES_URL}{foreign.pk}/", {}, format="json").status_code == 404


def test_applying_fills_placeholders_from_the_first_product(
    auth_client: Any, workspace: Any, user: Any, products: Any
) -> None:
    template = _template(workspace, user, product_ids=[p.pk for p in products])

    response = auth_client.post(f"{TEMPLATES_URL}{template.pk}/apply/")

    assert response.status_code == 201
    assert response.json()["master_body"] == "Happy hour: Mint tea -20%"
    assert response.json()["product"] == products[0].pk


# -----------------------------------------------------------------------------
# The form's options
# -----------------------------------------------------------------------------
def test_the_options_list_placeholders_products_and_accounts(
    auth_client: Any, products: Any, social_account: Any
) -> None:
    body = auth_client.get(OPTIONS_URL).json()

    assert {p["key"] for p in body["placeholders"]} >= {"product", "price", "link"}
    assert [p["id"] for p in body["products"]] == sorted(p.pk for p in products)
    assert body["products"][0]["has_photo"] is True
    assert [a["id"] for a in body["accounts"]] == [social_account.pk]
    assert {m["key"] for m in body["image_modes"]} == {"product_photo", "generate"}


def test_the_options_list_nothing_from_another_organization(auth_client: Any) -> None:
    theirs = _stranger_workspace("outside@example.com")
    Product.objects.create(workspace=theirs, name="Not yours")
    assert auth_client.get(OPTIONS_URL).json()["products"] == []


# -----------------------------------------------------------------------------
# Alerts
# -----------------------------------------------------------------------------
def test_credits_low_alerts_at_fifty_and_twenty_once_each(
    workspace: Any, user: Any, django_capture_on_commit_callbacks: Any
) -> None:
    _credit(workspace, user, 60)
    alerts = Notification.objects.filter(event_key=EventKey.CREDITS_LOW)

    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(9):  # 60 → 15 in steps of 5
            ledger.debit_credits(workspace, 5, quota=1000)
    assert sorted(n.payload["level"] for n in alerts) == [20, 50]

    with django_capture_on_commit_callbacks(execute=True):
        ledger.debit_credits(workspace, 5, quota=1000)
    assert alerts.count() == 2


def test_credit_alert_levels_are_the_plans_own_row(
    workspace: Any, user: Any, django_capture_on_commit_callbacks: Any
) -> None:
    plan = workspace.organization.plan
    plan.credit_alert_levels = [10]
    plan.save(update_fields=["credit_alert_levels"])
    _credit(workspace, user, 60)

    with django_capture_on_commit_callbacks(execute=True):
        ledger.debit_credits(workspace, 55, quota=1000)

    assert [n.payload["level"] for n in Notification.objects.filter(event_key="credits.low")] == [
        10
    ]


@time_machine.travel(NOW, tick=False)
def test_a_week_ahead_an_unready_slot_is_flagged_once(
    workspace: Any, user: Any, products: Any
) -> None:
    _rule(_template(workspace, user, product_ids=[products[0].pk]))
    recurrence.materialise_due()

    recurrence_warn()
    recurrence_warn()

    at_risk = Notification.objects.filter(event_key=EventKey.TEMPLATE_SLOT_AT_RISK)
    assert at_risk.count() == 1, "only the 6th is within the week, and it is told once"
    later = RecurrenceOccurrence.objects.get(warned_at__isnull=True)
    assert later.slot_at.astimezone(PARIS).date() == dt.date(2026, 3, 13)


@time_machine.travel(NOW, tick=False)
def test_an_approved_slot_is_not_flagged(workspace: Any, user: Any, products: Any) -> None:
    _rule(_template(workspace, user, product_ids=[products[0].pk]))
    recurrence.materialise_due()
    for occurrence in _made():
        approvals.approve(occurrence.post, actor=user)

    recurrence_warn()

    assert not Notification.objects.filter(event_key=EventKey.TEMPLATE_SLOT_AT_RISK).exists()


# -----------------------------------------------------------------------------
# Flag off is today's templates
# -----------------------------------------------------------------------------
@pytest.fixture
def flag_off(workspace: Any) -> None:
    FeatureFlag.objects.create(
        organization=workspace.organization, key=TEMPLATE_RULES_S18, enabled=False
    )


@pytest.mark.usefixtures("flag_off")
def test_with_the_flag_off_the_options_are_404(auth_client: Any) -> None:
    assert auth_client.get(OPTIONS_URL).status_code == 404


@pytest.mark.usefixtures("flag_off")
def test_with_the_flag_off_the_rules_are_dropped_on_save(auth_client: Any, products: Any) -> None:
    response = _save(auth_client, master_body="{product}", product_ids=[products[0].pk])
    assert response.status_code == 201
    assert "product_ids" not in response.json()["payload"]


@pytest.mark.usefixtures("flag_off")
@time_machine.travel(NOW, tick=False)
def test_with_the_flag_off_a_slot_is_one_plain_draft(
    workspace: Any, user: Any, products: Any
) -> None:
    rule = _rule(_template(workspace, user, product_ids=[p.pk for p in products]))

    assert recurrence.materialise_due() == len(recurrence.planned_slots(rule))
    post = _made()[0].post
    assert (post.status, post.product_id, post.proposed_scheduled_at) == ("DRAFT", None, None)
