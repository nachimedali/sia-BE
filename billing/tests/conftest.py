"""Fixtures shared by the billing suite.

Both of these were copied into a file at a time until every billing test module
had its own identical version. They live here instead, so a change to how a
workspace is provisioned — or to what the fake gateway records — is one edit.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from billing.gateways.fake import _fake_gateway


@pytest.fixture
def workspace(plans: Any, user: Any) -> Any:
    from workspaces.services.provisioning import provision_workspace

    return provision_workspace(user, name="Acme Studio")


@pytest.fixture(autouse=True)
def _reset_gateway() -> Iterator[None]:
    """The fake gateway is module-level so a test can inspect calls after a view
    has run, which makes its call log process-wide state like Redis is."""
    _fake_gateway.clear()
    yield
    _fake_gateway.clear()
