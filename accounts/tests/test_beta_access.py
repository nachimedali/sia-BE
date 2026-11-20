"""Beta access requests: the launch-month form on the public site (vitrine).

Public and unauthenticated, like registration: captcha first, a per-IP
throttle, then one row per email address. Staff hear about each submission by
email. There is no tenant here, so there is no cross-organization or
cross-workspace case to test — the endpoint reads and writes no tenant row.
"""

from __future__ import annotations

from typing import Any

import pytest
import time_machine
from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import BetaAccessRequest
from billing.models import FeatureFlag
from billing.services.flags import BETA_ACCESS
from common.throttling import BetaAccessThrottle

pytestmark = pytest.mark.django_db

URL = "/api/v1/beta-access/"
STAFF = "compose@semaphore-labs.com"


def submit(client: APIClient | None = None, ip: str = "203.0.113.7", **overrides: Any) -> Any:
    payload = {"email": "Lina@Brand.TN", "package": "growth", **overrides}
    return (client or APIClient()).post(URL, payload, format="json", REMOTE_ADDR=ip)


def test_a_request_is_recorded_with_its_package(plans) -> None:
    response = submit(
        business_name="Dar Lina",
        phone="+216 20 123 456",
        social_url="https://instagram.com/darlina",
        currency="TND",
    )

    assert response.status_code == 202, response.json()
    assert response.json() == {"received": True}
    row = BetaAccessRequest.objects.get()
    assert row.email == "lina@brand.tn"
    assert row.plan == plans["growth"]
    assert row.business_name == "Dar Lina"
    assert row.phone == "+216 20 123 456"
    assert row.social_url == "https://instagram.com/darlina"
    assert row.currency == "TND"
    assert row.submissions == 1


def test_the_optional_fields_may_be_left_out(plans) -> None:
    assert submit().status_code == 202
    row = BetaAccessRequest.objects.get()
    assert (row.business_name, row.phone, row.social_url, row.currency) == ("", "", "", "")


@override_settings(BETA_ACCESS_NOTIFY_EMAIL=STAFF)
def test_staff_are_emailed_each_request(plans, outbox, django_capture_on_commit_callbacks) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        submit(business_name="Dar Lina")

    assert len(outbox) == 1
    email = outbox[0]
    assert email.to == STAFF
    assert email.template == "beta_access_request"
    assert email.context["email"] == "lina@brand.tn"
    assert email.context["package"] == "Growth"
    assert email.context["business_name"] == "Dar Lina"


@override_settings(BETA_ACCESS_NOTIFY_EMAIL="")
def test_no_staff_address_still_records_the_request(
    plans, outbox, django_capture_on_commit_callbacks
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        assert submit().status_code == 202

    assert BetaAccessRequest.objects.count() == 1
    assert outbox == []


def test_a_repeat_updates_the_same_row(plans) -> None:
    submit(package="discovery")
    response = submit(email="lina@brand.tn", package="scale", phone="+216 98 000 000")

    assert response.status_code == 202
    row = BetaAccessRequest.objects.get()
    assert row.plan == plans["scale"]
    assert row.phone == "+216 98 000 000"
    assert row.submissions == 2


def test_a_repeat_keeps_fields_it_leaves_blank(plans) -> None:
    submit(business_name="Dar Lina")
    submit(business_name="")
    assert BetaAccessRequest.objects.get().business_name == "Dar Lina"


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": ""},
        {"email": "not-an-email"},
        {"package": ""},
        {"package": "nonexistent"},
        {"social_url": "not a link"},
        {"social_url": "javascript:alert(1)"},
        {"phone": "1" * 41},
        {"currency": "EUR"},
    ],
)
def test_invalid_input_is_a_validation_error(plans, overrides: dict[str, Any]) -> None:
    response = submit(**overrides)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert not BetaAccessRequest.objects.exists()


def test_a_package_off_the_public_list_is_refused(plans) -> None:
    plans["growth"].is_public = False
    plans["growth"].save(update_fields=["is_public"])

    response = submit(package="growth")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_a_failed_captcha_is_refused_before_anything_is_written(plans) -> None:
    response = submit(captcha="fail")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "captcha_failed"
    assert not BetaAccessRequest.objects.exists()


def test_requests_are_rate_limited_per_ip(plans) -> None:
    client = APIClient()
    with time_machine.travel(0, tick=False):
        for i in range(BetaAccessThrottle.capacity):
            assert submit(client, email=f"a{i}@brand.tn").status_code == 202

        blocked = submit(client, email="late@brand.tn")

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked["Retry-After"]) >= 1


def test_flag_off_answers_not_found(plans) -> None:
    FeatureFlag.objects.create(organization=None, key=BETA_ACCESS, enabled=False)

    response = submit()

    assert response.status_code == 404
    assert not BetaAccessRequest.objects.exists()


def test_the_admin_exports_requests_as_csv_without_formulas(plans, client) -> None:
    from accounts.models import User

    submit(business_name="=HYPERLINK(1)", phone="+216 20 123 456")
    client.force_login(User.objects.create_superuser(email="staff@example.com", password="x"))

    response = client.post(
        "/admin/accounts/betaaccessrequest/",
        {"action": "export_csv", "_selected_action": [BetaAccessRequest.objects.get().pk]},
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv"
    body = response.content.decode()
    assert body.splitlines()[0].startswith("email,package,currency")
    assert "lina@brand.tn,growth" in body
    assert "'=HYPERLINK(1)" in body
    assert "'+216 20 123 456" in body
