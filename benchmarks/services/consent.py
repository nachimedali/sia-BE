"""Consent (P8-05, P8-07) — opt-in, against the current terms, and revocable at once.

**A workspace contributes only while its newest consent record is a grant of the
currently published policy.** Every clause of that sentence closes a way that
agreement could be assumed instead of given: no record means never asked; a
newer revocation means withdrawn; a grant of an older policy means agreement to
terms that have since changed.

Revocation removes the workspace's projected rows in the same transaction as
the record, rather than waiting for the nightly run. "Stops future
contribution" has to hold for an aggregation that happens to run five minutes
after the click. Benchmarks already computed are not touched (P8-07).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from rest_framework.exceptions import ValidationError

from benchmarks.markets import all_markets
from benchmarks.models import ConsentAction, ConsentPolicy, ConsentRecord
from benchmarks.services.contributors import withdraw
from common.exceptions import StateConflict
from workspaces.models import Workspace


class ConsentPolicyUnavailable(StateConflict):
    default_code = "consent_policy_unavailable"
    default_detail = "Benchmark participation terms have not been published yet."


class ConsentPolicyOutdated(StateConflict):
    default_code = "consent_policy_outdated"
    default_detail = "Those terms have been replaced. Review the current version first."


class BenchmarkProfileIncomplete(StateConflict):
    default_code = "benchmark_profile_incomplete"
    default_detail = "This workspace needs a category before it can join a benchmark cohort."


class ConsentNotGranted(StateConflict):
    default_code = "consent_not_granted"
    default_detail = "This workspace has not opted into benchmarks."


class BenchmarkConsentRequired(StateConflict):
    """409 rather than 403: nothing is wrong with the caller's role, and 403 is
    reserved for that (Part 3). The workspace is in a state that does not allow
    the read, and opting in is the transition that changes it."""

    default_code = "benchmark_consent_required"
    default_detail = "Benchmarks are available to workspaces that contribute to them."


@dataclass(frozen=True)
class ConsentStatus:
    policy: ConsentPolicy | None
    latest: ConsentRecord | None

    @property
    def granted(self) -> bool:
        return self.latest is not None and self.latest.action == ConsentAction.GRANTED

    @property
    def contributing(self) -> bool:
        return (
            self.granted
            and self.policy is not None
            and self.latest is not None
            and self.latest.policy_id == self.policy.pk
        )

    @property
    def requires_reconsent(self) -> bool:
        return self.granted and not self.contributing


def current_policy() -> ConsentPolicy | None:
    return ConsentPolicy.objects.order_by("-version").first()


def status(workspace: Workspace) -> ConsentStatus:
    latest = (
        ConsentRecord.objects.filter(workspace=workspace)
        .select_related("policy")
        .order_by("-recorded_at", "-id")
        .first()
    )
    return ConsentStatus(policy=current_policy(), latest=latest)


def is_contributing(workspace: Workspace) -> bool:
    return status(workspace).contributing


def normalise_market(value: Any) -> str:
    code = value.strip().upper() if isinstance(value, str) else ""
    if code not in all_markets():
        raise ValidationError({"market": "Choose a market by its two-letter country code."})
    return code


@transaction.atomic
def grant(workspace: Workspace, *, actor: Any, policy_version: int, market: str) -> ConsentRecord:
    """Opt in under `policy_version`, which must be the current policy.

    Asking for the version rather than assuming it is how the record proves the
    workspace agreed to what it was shown: a page loaded before new terms were
    published submits the old version and is refused, instead of silently
    agreeing to terms nobody on that page saw.
    """
    # Serialises concurrent grants, so a double-click writes one record.
    Workspace.objects.select_for_update().get(pk=workspace.pk)

    policy = current_policy()
    if policy is None:
        raise ConsentPolicyUnavailable()
    if policy_version != policy.version:
        raise ConsentPolicyOutdated(detail={"current_version": policy.version})
    if workspace.category_id is None:
        raise BenchmarkProfileIncomplete(detail={"missing": ["category"]})

    code = normalise_market(market)
    if workspace.market != code:
        workspace.market = code
        workspace.save(update_fields=["market", "updated_at"])

    current = status(workspace)
    if current.contributing and current.latest is not None:
        return current.latest

    return ConsentRecord.objects.create(
        workspace=workspace,
        policy=policy,
        action=ConsentAction.GRANTED,
        actor=actor if getattr(actor, "pk", None) else None,
    )


@transaction.atomic
def revoke(workspace: Workspace, *, actor: Any) -> ConsentRecord:
    Workspace.objects.select_for_update().get(pk=workspace.pk)

    current = status(workspace)
    if not current.granted or current.latest is None:
        raise ConsentNotGranted()

    record = ConsentRecord.objects.create(
        workspace=workspace,
        policy=current.latest.policy,
        action=ConsentAction.REVOKED,
        actor=actor if getattr(actor, "pk", None) else None,
    )
    withdraw(workspace.pk)
    return record
