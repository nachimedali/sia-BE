from collections.abc import Iterator

import pytest

from common.redis import get_redis


@pytest.fixture(autouse=True)
def _flush_test_redis() -> Iterator[None]:
    """Redis is real in tests (see config/settings/test.py), so isolate keys.

    Runs against DB 15, which is reserved for the suite.
    """
    client = get_redis()
    client.flushdb()
    yield
    client.flushdb()
