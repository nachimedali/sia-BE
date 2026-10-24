"""Deploy checks that are not any one app's (Django's system-check framework).

Tagged `deploy`, so they run on `manage.py check --deploy`, which is where a
release must stop.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, Warning, register

#: Fakes a deployment may keep for now, each with the reason it is not an error.
#: Billing only in refusing form (F1); the report renderer writes the real
#: computed numbers as JSON because no PDF engine ships yet (P6-05).
TOLERATED = {"USE_FAKE_BILLING", "USE_FAKE_REPORT_RENDERER", "USE_FAKE_CAPTCHA"}
#: Owned by another app's deploy check (`social_providers.E003`), which also
#: knows what its `None` (auto) means; one cause, one error.
OWNED_ELSEWHERE = {"USE_FAKE_PLATFORM_ADAPTER"}


@register(Tags.security, deploy=True)
def check_no_fakes_in_deploy(app_configs: Any, **kwargs: Any) -> list[CheckMessage]:
    """Every `USE_FAKE_*` that is on, read from settings rather than a list so a
    fake added later is covered too (Part 7 rule 17)."""
    on = sorted(
        name
        for name in dir(settings)
        if name.startswith("USE_FAKE_") and name not in OWNED_ELSEWHERE and getattr(settings, name)
    )
    messages: list[CheckMessage] = []
    refusing = getattr(settings, "FAKE_BILLING_REFUSES", False)
    blocking = [n for n in on if n not in TOLERATED or (n == "USE_FAKE_BILLING" and not refusing)]
    if blocking:
        messages.append(
            Error(
                f"A deployment cannot run on fakes: {', '.join(blocking)}.",
                hint="Set each to False and configure the real provider.",
                id="common.E010",
            )
        )
    if "USE_FAKE_BILLING" in on and refusing:
        messages.append(
            Warning(
                "No payment provider: checkout, portal and the Stripe webhook answer 503.",
                hint="Set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET (critical_wait.md W1).",
                id="common.W010",
            )
        )
    if "USE_FAKE_REPORT_RENDERER" in on:
        messages.append(
            Warning(
                "No PDF engine: report documents are written as JSON.",
                id="common.W011",
            )
        )
    if "USE_FAKE_CAPTCHA" in on:
        messages.append(
            Warning(
                "No captcha: register, login, password reset and token pages accept any token.",
                hint=(
                    "Set TURNSTILE_SECRET_KEY here and NEXT_PUBLIC_TURNSTILE_SITE_KEY "
                    "on the frontend."
                ),
                id="common.W012",
            )
        )
    return messages
