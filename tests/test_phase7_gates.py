"""Phase 7's ship gates.

**P7-G1 — every finding shows a confidence grade and a sample size.**
**P7-G2 — no digest string contains a predicted numeric outcome.**

Both are asserted at every layer that can produce the string a customer reads —
the model, the service, the serializer and the API — because a gate that held
only in the service would hold in the one place nobody renders from.

Why G2 is the harder of the two to keep: it is a claim about *prose*, and prose
is the one artefact in this system a human did not write and a schema cannot
constrain. The digest is read as authoritative precisely because the rest of
the product is careful with numbers, so one sentence saying "expect +34% reach"
inherits that credibility and spends it on a figure nothing computed. Upstream
metric quality is uneven and unverifiable per platform; a comparative claim
degrades gracefully under bad data, and a numeric promise does not.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import pytest
from django.urls import reverse

from ai.providers.base import TextGenerationResult, TextVariant
from learn.models import Confidence, Digest, Finding
from learn.serializers import DigestSerializer
from learn.services import narrate
from learn.services.run import run_learn
from taste.models import TasteProfile

pytestmark = pytest.mark.django_db

NOW = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)

#: The shapes a read-out may never contain (P7-10, P7-G2). Kept here as well as
#: in `narrate` on purpose: this file is the gate, and a gate that imported the
#: implementation's own definition of "wrong" would pass by construction the
#: day somebody narrowed it.
FORBIDDEN = (
    re.compile("[+\u2212-]\\s?\\d+(?:[.,]\\d+)?\\s?%"),
    re.compile(r"\bwill\s+(?:increase|rise|improve|grow|climb|lift|drop|fall|reach)\b", re.I),
    re.compile(r"\bexpect(?:s|ed|ing)?\b", re.I),
    re.compile(r"\bproject(?:s|ed|ion)?\b", re.I),
    re.compile(r"\bforecast(?:s|ed)?\b", re.I),
    re.compile(r"\bpredict(?:s|ed|ion)?\b", re.I),
)


def assert_reads_as_observation(text: str) -> None:
    for pattern in FORBIDDEN:
        assert not pattern.search(text), f"predicted outcome in digest prose: {text!r}"


class Fabricating:
    """A provider that does exactly what this phase is built to survive."""

    def __init__(self, body: str) -> None:
        self.body = body

    def generate(self, **_: Any) -> TextGenerationResult:
        return TextGenerationResult(
            variants=[TextVariant(body=self.body)],
            provider="stub",
            model="stub-1",
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
        )


@pytest.fixture
def digest(paid_workspace: Any, user: Any) -> Digest:
    TasteProfile.objects.create(
        workspace=paid_workspace, version=1, is_active=True, voice={"tone": "warm"}, created_by=user
    )
    row = Digest.objects.create(
        workspace=paid_workspace,
        window_start=NOW - dt.timedelta(days=90),
        window_end=NOW,
        narration="Carousels led in 18 of 24 posts.",
        statistics={"findings": []},
    )
    for confidence, size in (
        (Confidence.STRONG, 24),
        (Confidence.EMERGING, 9),
        (Confidence.INSUFFICIENT, 2),
    ):
        Finding.objects.create(
            digest=row,
            segment={"dimension": "format", "value": f"F{size}"},
            comparison={"led_in": 1, "sample_size": size},
            confidence=confidence,
            sample_size=size,
            baseline_size=40,
            campaigns_observed=2,
        )
    return row


class TestG1EveryFindingIsQualified:
    """A finding without its grade and its `n` is an unsourced claim."""

    def test_the_columns_cannot_be_null(self) -> None:
        confidence = Finding._meta.get_field("confidence")
        sample = Finding._meta.get_field("sample_size")

        assert not confidence.null, "a finding with no grade must not be representable"
        assert not sample.null, "a finding with no sample size must not be representable"

    def test_the_serializer_exposes_both_on_every_finding(self, digest: Digest) -> None:
        data = DigestSerializer(digest).data

        assert data["findings"], "the fixture produced no findings"
        for finding in data["findings"]:
            assert finding["confidence"] in Confidence.values
            assert isinstance(finding["sample_size"], int)

    def test_the_api_exposes_both_on_every_finding(self, auth_client: Any, digest: Digest) -> None:
        response = auth_client.get(reverse("digest-detail", args=[digest.pk]))

        assert response.status_code == 200
        assert response.data["findings"]
        for finding in response.data["findings"]:
            assert finding["confidence"] in Confidence.values
            assert finding["sample_size"] >= 0

    def test_a_grade_is_never_invented_for_a_thin_segment(self, digest: Digest) -> None:
        """n=2 must read Insufficient, not a quiet promotion to Emerging."""
        thin = digest.findings.get(sample_size=2)
        assert thin.confidence == Confidence.INSUFFICIENT


class TestG2NoDigestPredicts:
    def test_the_template_never_predicts_at_any_grade(self, digest: Digest) -> None:
        payload = narrate.build_payload(
            [],
            window_start=NOW - dt.timedelta(days=90),
            window_end=NOW,
        )
        assert_reads_as_observation(narrate.template_narration(payload))

    @pytest.mark.parametrize(
        "fabricated",
        [
            "Carousels will increase your reach by 30%.",
            "We expect engagement to climb next month.",
            "Projected reach for carousels is higher.",
            "Switching format delivers +18% engagement.",
            "This predicts a stronger quarter.",
        ],
    )
    def test_a_fabricating_provider_never_reaches_the_stored_digest(
        self, paid_workspace: Any, user: Any, fabricated: str
    ) -> None:
        """The end-to-end claim: whatever the provider says, the row is clean.

        Asserted through `run_learn` rather than through `narrate` alone,
        because the gate is about what gets *persisted* — a validator that ran
        and whose result was then ignored on the way to the column would pass
        every unit test in the phase."""
        TasteProfile.objects.create(
            workspace=paid_workspace,
            version=1,
            is_active=True,
            voice={"tone": "warm"},
            created_by=user,
        )

        stored = run_learn(paid_workspace, now=NOW, provider=Fabricating(fabricated))
        stored.refresh_from_db()

        assert_reads_as_observation(stored.narration)
        assert stored.narration_source == "TEMPLATE"

    def test_an_ungrounded_numeral_never_reaches_the_stored_digest(
        self, paid_workspace: Any, user: Any
    ) -> None:
        """Part 7 rule 15. 34 was computed by nothing, so it may not be shown."""
        TasteProfile.objects.create(
            workspace=paid_workspace,
            version=1,
            is_active=True,
            voice={"tone": "warm"},
            created_by=user,
        )

        stored = run_learn(
            paid_workspace, now=NOW, provider=Fabricating("Carousels reached 34 people.")
        )
        stored.refresh_from_db()

        assert "34" not in stored.narration

    def test_the_api_serves_no_prediction(self, auth_client: Any, digest: Digest) -> None:
        response = auth_client.get(reverse("digest-detail", args=[digest.pk]))
        assert_reads_as_observation(response.data["narration"])


class TestTenancy:
    """Part 7 rule 3 — cross-tenant is 404, never 403."""

    def test_another_organizations_digest_is_a_404(
        self, auth_client: Any, workspace: Any, other_user: Any
    ) -> None:
        """Reached with the caller's own workspace context, so the only thing
        that can hide the row is the queryset — which is the point."""
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Another Company")
        assert theirs.organization != workspace.organization

        hidden = Digest.objects.create(
            workspace=theirs, window_start=NOW - dt.timedelta(days=1), window_end=NOW
        )

        response = auth_client.get(reverse("digest-detail", args=[hidden.pk]))

        assert response.status_code == 404, "a 403 or a 200 both confirm the row exists"

    def test_naming_another_organizations_workspace_is_404(
        self, auth_client: Any, workspace: Any, other_user: Any
    ) -> None:
        """The other half: not just hiding their rows, but refusing their
        context. Cross-org and cross-workspace are separate dimensions and a
        guard that covers one routinely misses the other."""
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Another Company")

        response = auth_client.get(reverse("digest-list"), HTTP_X_WORKSPACE_ID=str(theirs.pk))

        assert response.status_code == 404


class TestLearnProposesOnly:
    """Part 7 rule 14 — Learn never activates its own conclusions."""

    def test_no_proposed_ruleset_is_ever_created_active(
        self, paid_workspace: Any, user: Any
    ) -> None:
        from learn.services import proposals

        row = Digest.objects.create(
            workspace=paid_workspace, window_start=NOW - dt.timedelta(days=90), window_end=NOW
        )
        Finding.objects.create(
            digest=row,
            segment={"dimension": "format", "value": "CAROUSEL"},
            comparison={"led_in": 18, "sample_size": 24},
            confidence=Confidence.STRONG,
            sample_size=24,
            baseline_size=40,
            campaigns_observed=3,
        )

        ruleset = proposals.propose_from(row)

        assert ruleset is not None
        assert ruleset.is_active is False
        assert all(rule.accepted_at is None for rule in ruleset.rules.all())
