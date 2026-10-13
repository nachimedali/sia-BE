from __future__ import annotations

from collections.abc import Iterator

import pytest

from social_providers.socialapi.budget import TeamBudget


@pytest.fixture
def budget(settings: object) -> Iterator[TeamBudget]:
    """A clean team budget per test. Redis is shared across a parallel run, so
    each test starts from an empty bucket and no holds."""
    team = TeamBudget()
    team.reset()
    yield team
    team.reset()
