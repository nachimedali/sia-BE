"""S17-13 — the template script writer (`AD_SCRIPT`).

`POST /ai/ad-templates/script/` asks the text model for the template's hook,
body and call to action, for this product, in the content language. Paid in
credits, debited only when the model's words pass their checks; otherwise the
template's own words come back and nothing is taken.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai.providers.base import TextGenerationResult, TextVariant
from ai.providers.fake import _fake_text_provider
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    catalog,
    code_of,
    flag_on,
    growth,
    make_product,
    on_plan,
)
from billing.models import FeatureFlag
from billing.services import ledger
from billing.services.flags import AD_TEMPLATES_S17
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

SCRIPT = "/api/v1/ai/ad-templates/script/"


@pytest.fixture
def oil(growth: Any, make_png_upload: Any) -> Any:  # noqa: F811
    return make_product(growth, make_png_upload)


def ask(client: Any, product: Any, **extra: Any) -> Any:
    return client.post(
        SCRIPT,
        {"template": "pour-shot", "product": product.pk, "language": "fr", **extra},
        format="json",
    )


def answer(monkeypatch: Any, body: str) -> None:
    def generate(**kwargs: Any) -> TextGenerationResult:
        _fake_text_provider.calls.append(kwargs)
        return TextGenerationResult(
            variants=[TextVariant(body=body)],
            provider="fake",
            model="fake",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )

    monkeypatch.setattr(_fake_text_provider, "generate", generate)


def test_the_model_writes_hook_body_and_cta_in_the_language_and_is_paid_once(
    auth_client: Any, oil: Any
) -> None:
    before = ledger.credit_balance(oil.workspace)
    response = ask(auth_client, oil)

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["by"] == "model" and body["credits"] == 1
    assert body["hook"] and body["body"] and body["cta"]
    assert ledger.credit_balance(oil.workspace) == before - 1
    call = _fake_text_provider.calls[-1]
    assert "French" in call["prompt"] and "Olive oil 500 ml" in call["prompt"]


def test_words_that_fail_their_checks_give_back_the_templates_own_free(
    auth_client: Any, oil: Any, monkeypatch: Any
) -> None:
    # A number the brief never gave is an invented claim.
    answer(monkeypatch, "HOOK: 97% of chefs agree.\nBODY: The best oil.\nCTA: Buy now.")
    before = ledger.credit_balance(oil.workspace)
    body = ask(auth_client, oil).json()

    assert body["by"] == "template" and body["credits"] == 0
    assert body["hook"] == "This is the pour."  # the row's own words
    assert ledger.credit_balance(oil.workspace) == before

    answer(monkeypatch, "Sure! Here is a script.")  # not the three lines asked for
    assert ask(auth_client, oil).json()["by"] == "template"


def test_no_credits_is_402_before_any_model_call(auth_client: Any, oil: Any) -> None:
    ledger.adjust_credits(
        oil.workspace, -ledger.credit_balance(oil.workspace), actor=None, note="empty"
    )
    response = ask(auth_client, oil)
    assert response.status_code == 402
    assert not _fake_text_provider.calls


def test_a_template_above_the_plan_is_402(auth_client: Any, oil: Any, plans: Any) -> None:
    on_plan(oil.workspace, plans, "spark")
    response = ask(auth_client, oil, template="first-look")  # a person on screen
    assert response.status_code == 402
    assert code_of(response) == "feature_not_available"
    assert not _fake_text_provider.calls


def test_another_brand_or_organizations_product_is_404_and_flag_off_is_404(
    auth_client: Any, oil: Any, user: Any, other_user: Any, make_png_upload: Any
) -> None:
    here = {"HTTP_X_WORKSPACE_ID": str(oil.workspace.pk)}
    for workspace in (
        provision_workspace(other_user, name="Stranger"),
        provision_extra_workspace(user=user, name="Sibling"),
    ):
        theirs = make_product(workspace, make_png_upload)
        response = auth_client.post(
            SCRIPT, {"template": "pour-shot", "product": theirs.pk}, format="json", **here
        )
        assert response.status_code == 404

    FeatureFlag.objects.create(
        organization=oil.workspace.organization, key=AD_TEMPLATES_S17, enabled=False
    )
    response = auth_client.post(
        SCRIPT, {"template": "pour-shot", "product": oil.pk}, format="json", **here
    )
    assert response.status_code == 404
