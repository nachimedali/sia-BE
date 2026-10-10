"""Port resolution for the trendgen seam.

One mechanism, instantiated per port, rather than the same override/flag/local
ladder copied into each module. A third port is a `Seam(...)` constructor call;
before this it was eighteen duplicated lines plus two more conftest fixtures.

Resolution order: a test override, then the org's rollout flag, then the
in-process adapter. Flag on before the remote adapter exists raises rather than
degrading — a premature flip should be loud.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from billing.services.flags import flag_enabled

if TYPE_CHECKING:
    from collections.abc import Callable

    from workspaces.models import Organization

#: Every seam built, so the test fixture can reset them all without naming one.
REGISTRY: list[Seam[Any]] = []


class Seam[T]:
    def __init__(self, *, flag: str, local: Callable[[], T], remote_phase: str) -> None:
        self.flag = flag
        self.remote_phase = remote_phase
        # The local adapters are stateless, so one instance serves every call
        # rather than allocating per resolution on the generation hot path.
        self._local = local()
        self._override: T | None = None
        REGISTRY.append(self)

    def resolve(self, organization: Organization) -> T:
        if self._override is not None:
            return self._override
        if flag_enabled(organization, self.flag):
            raise NotImplementedError(
                f"The trendgen HTTP adapter for '{self.flag}' ships in {self.remote_phase}. "
                f"Disable the '{self.flag}' flag to stay in process."
            )
        return self._local

    def override(self, port: T | None) -> None:
        """Install a fake for the duration of a test."""
        self._override = port


def reset_all() -> None:
    for seam in REGISTRY:
        seam.override(None)
