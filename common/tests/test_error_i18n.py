"""X-01 — the API's fixed error messages follow the page's language (L-6).

`build_envelope` swaps each message for its French or Italian words
(`common.error_i18n.ERRORS`) under the request's `Accept-Language`. Every
`OCCSError` subclass's `default_detail` has its words, so a new error class
cannot ship English-only to a French page.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
from django.conf import settings
from django.utils import translation

from common.error_i18n import ERRORS
from common.exceptions import OCCSError, build_envelope


def _subclasses(cls: type[OCCSError]) -> Iterator[type[OCCSError]]:
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def test_every_error_class_has_its_words_in_french_and_italian() -> None:
    root = str(Path(settings.BASE_DIR))
    for app in apps.get_app_configs():
        if not app.path.startswith(root):
            continue
        for module in pkgutil.walk_packages([app.path], f"{app.name}."):
            if not any(
                part in module.name for part in (".tests", ".migrations", ".management", ".admin")
            ):
                importlib.import_module(module.name)

    classes: list[type[OCCSError]] = [OCCSError, *_subclasses(OCCSError)]
    missing = sorted({str(cls.default_detail) for cls in classes} - set(ERRORS))
    assert missing == []
    assert all(fr.strip() and it.strip() for fr, it in ERRORS.values())


def test_an_envelope_speaks_the_active_language_and_leaves_one_off_words_alone() -> None:
    with translation.override("fr"):
        assert build_envelope("x", "Too many requests.")["error"]["message"] == "Trop de demandes."
        assert build_envelope("x", "Something said once.")["error"]["message"] == (
            "Something said once."
        )
    with translation.override("en"):
        assert build_envelope("x", "Too many requests.")["error"]["message"] == "Too many requests."


@pytest.mark.django_db
def test_a_refusal_reaches_an_italian_page_in_italian(auth_client: Any) -> None:
    response = auth_client.post(
        "/api/v1/auth/me/password/",
        {"current": "wrong", "new": "a-new-long-pass-1"},
        format="json",
        HTTP_ACCEPT_LANGUAGE="it",
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["message"] == "Questa non è la tua password attuale."
    assert error["detail"]["fields"]["current"] == ["Questa non è la tua password attuale."]
