"""Shared `httpx` stubbing for the provider contract suites.

One copy, imported by every contract test that has a real adapter to cover
(`ai/tests/test_provider_contract.py`, `channels/tests/test_adapter_contract.py`)
— the frontend collapsed its own three copies of the same idea into
`test/http.ts` for the same reason. A change to how `httpx` has to be patched is
then one edit rather than one per provider, and the copy nobody remembered to
update cannot fail in a way that looks like a provider bug.
"""

from __future__ import annotations

from typing import Any

import httpx


def patch_httpx_transport(monkeypatch: Any, handler: Any) -> None:
    """Swaps only the network transport, leaving each adapter's own `_client()`
    logic — base URL, auth header, timeout — running for real. Patching
    `_client()` itself would bypass exactly the parameter assembly these
    contract tests exist to cover."""
    original_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
