"""`manage.py check --deploy` refuses a deployment that runs on a fake (F1, rule 17).

The check reads every `USE_FAKE_*` setting rather than a list, so a fake added
later is covered without anyone remembering to add it here.
"""

from __future__ import annotations

from django.conf import settings as django_settings

from common.checks import check_no_fakes_in_deploy


def _ids() -> list[str]:
    return [message.id for message in check_no_fakes_in_deploy(None)]


def _all_off(settings) -> None:
    for name in dir(django_settings):
        if name.startswith("USE_FAKE_"):
            setattr(settings, name, False)
    settings.FAKE_BILLING_REFUSES = False


def test_a_deployment_on_no_fake_passes(settings) -> None:
    _all_off(settings)

    assert _ids() == []


def test_any_fake_left_on_is_an_error_named_after_its_setting(settings) -> None:
    _all_off(settings)
    settings.USE_FAKE_SITE_FETCHER = True
    settings.USE_FAKE_SOMETHING_NEW = True

    messages = check_no_fakes_in_deploy(None)

    assert {m.id for m in messages} == {"common.E010"}
    text = " ".join(m.msg for m in messages)
    assert "USE_FAKE_SITE_FETCHER" in text
    assert "USE_FAKE_SOMETHING_NEW" in text


def test_the_fake_payment_gateway_is_an_error_unless_it_refuses(settings) -> None:
    _all_off(settings)
    settings.USE_FAKE_BILLING = True

    assert _ids() == ["common.E010"]

    settings.FAKE_BILLING_REFUSES = True
    assert _ids() == ["common.W010"]


def test_the_report_renderer_warns_because_no_engine_exists_yet(settings) -> None:
    """The fake renders the real computed numbers as JSON — not invented data,
    just not a PDF — so it is a warning until an engine ships."""
    _all_off(settings)
    settings.USE_FAKE_REPORT_RENDERER = True

    assert _ids() == ["common.W011"]


def test_the_platform_adapter_is_left_to_the_providers_own_check(settings) -> None:
    """`social_providers.E003` owns that rule, including what `None` (auto)
    means; reporting it here too would be two errors for one cause."""
    _all_off(settings)
    for value in (None, True):
        settings.USE_FAKE_PLATFORM_ADAPTER = value
        assert _ids() == []
