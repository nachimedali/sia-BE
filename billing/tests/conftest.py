"""Fixtures shared by the billing suite.

`workspace` used to live here too, copied from file to file until this module
collapsed them into one; it has since moved to the root conftest, because
content/tests needed the exact same fixture (same reasoning, one app wider).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from billing.gateways.fake import _fake_gateway


@pytest.fixture(autouse=True)
def _reset_gateway() -> Iterator[None]:
    """The fake gateway is module-level so a test can inspect calls after a view
    has run, which makes its call log process-wide state like Redis is."""
    _fake_gateway.clear()
    yield
    _fake_gateway.clear()
