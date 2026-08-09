"""The evaluation harness (design.md §8.3, implementation.md Phase 7.8).

A fixed benchmark set of products x prompts, run through the real pipeline
(grounding, provider call, quality gate) and scored, so a prompt or provider
change that quietly degrades output shows up as a number moving, not as a
support ticket three weeks later. "Quality is a tracked number that can
regress visibly... a regression test, not polish" (design.md §8.3) — which is
why `ai/tests/test_eval_harness.py` runs this in the normal suite (and so
CI) rather than only existing as a script someone has to remember to run.

Runs against the fake providers, deliberately: this environment has no
provider credentials, and a benchmark that only worked with real ones
would not be the "runs on every change under ai/" regression test design.md
asks for. It is still a meaningful signal against the fakes — the quality
gate, prompt assembly, and costing are all real code; only the provider call
itself is swapped, exactly per the port/fake contract (A8) every other
external dependency in this codebase follows. A `@pytest.mark.integration`
run against real providers is the complementary check design.md §9 already
establishes the shape for.
"""

from __future__ import annotations

import io
import statistics
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from accounts.models import User
from ai.models import GenerationKind, GenerationMode, GenerationStatus
from ai.services.pipeline import create_generation, run_generation
from billing.models import Plan
from billing.services.ledger import grant_credits
from products.models import Product
from products.services.products import attach_reference_images, create_product
from workspaces.models import Workspace
from workspaces.services.provisioning import provision_workspace

BENCHMARK_WORKSPACE_NAME = "OCCS Eval Harness"


@dataclass(frozen=True)
class EvalCase:
    name: str
    kind: str
    mode: str
    prompt: str
    with_product: bool = False
    restrictions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvalCaseResult:
    name: str
    passed: bool
    attempts: int
    identity_score: float | None
    credits_charged: int


@dataclass(frozen=True)
class EvalResult:
    cases: list[EvalCaseResult]

    @property
    def pass_rate(self) -> float:
        return sum(1 for c in self.cases if c.passed) / len(self.cases)

    @property
    def mean_identity_score(self) -> float | None:
        scores = [c.identity_score for c in self.cases if c.identity_score is not None]
        return statistics.mean(scores) if scores else None

    @property
    def mean_attempts(self) -> float:
        return statistics.mean(c.attempts for c in self.cases)

    def as_dict(self) -> dict[str, object]:
        return {
            "pass_rate": round(self.pass_rate, 4),
            "mean_identity_score": (
                round(self.mean_identity_score, 4) if self.mean_identity_score is not None else None
            ),
            "mean_attempts": round(self.mean_attempts, 4),
            "cases": {
                c.name: {
                    "passed": c.passed,
                    "attempts": c.attempts,
                    "identity_score": c.identity_score,
                    "credits_charged": c.credits_charged,
                }
                for c in self.cases
            },
        }


BENCHMARK_CASES: list[EvalCase] = [
    EvalCase(
        name="text_idea_no_product",
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="a cosy morning with your favourite mug",
    ),
    EvalCase(
        name="text_product_with_restriction",
        kind=GenerationKind.TEXT,
        mode=GenerationMode.PRODUCT,
        prompt="launch day announcement",
        with_product=True,
        restrictions=["always mention handmade"],
    ),
    EvalCase(
        name="image_product_on_table",
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="on a sunlit table",
        with_product=True,
    ),
    EvalCase(
        name="image_product_with_hard_constraint",
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="styled for a product page",
        with_product=True,
        restrictions=["always show the handle"],
    ),
]


def _reference_upload() -> SimpleUploadedFile:
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), color=(90, 140, 200)).save(buffer, format="PNG")
    return SimpleUploadedFile("reference.png", buffer.getvalue(), content_type="image/png")


def _benchmark_workspace() -> tuple[User, Workspace]:
    """A permanent fixture account, reused across runs rather than created
    and torn down each time.

    Every generation here debits real `CreditLedger`/`VideoLedger` rows, and
    those are append-only forever (I4) — even a `Workspace.delete()` cascade
    cannot remove them, because the DB trigger backing I4 (A34) forbids
    UPDATE *and* DELETE on the ledger tables unconditionally, no exception
    for "this row is being deleted together with what it depends on." A
    teardown step here would not degrade gracefully; it would crash the
    harness. So there is no teardown — the harness account simply
    accumulates history the same way any real workspace's ledger does
    (design.md §15.8 A75)."""
    user, _ = get_user_model().objects.get_or_create(
        email="eval-harness@occs.internal", defaults={"is_active": True}
    )
    workspace = Workspace.objects.filter(owner=user).order_by("created_at").first()
    if workspace is None:
        workspace = provision_workspace(user, name=BENCHMARK_WORKSPACE_NAME)

    # Advanced, not Free: the benchmark needs more than Free's one-product
    # quota (§4.1), and this is internal tooling, not a real customer this
    # quota is meant to constrain.
    advanced = Plan.objects.filter(code="advanced").first()
    if advanced is not None and workspace.plan_id != advanced.id:
        workspace.plan = advanced
        workspace.save(update_fields=["plan", "updated_at"])

    grant_credits(workspace, 100, note="eval harness")
    return user, workspace


def _benchmark_product(workspace: Workspace, case: EvalCase) -> Product | None:
    """Reused by name across runs, for the same reason the workspace is —
    an unbounded number of near-identical products is just noise, not a
    more thorough benchmark.

    A reference image is attached regardless of `case.kind` — I7 gates
    generation on `Product.is_generation_ready`, and that check runs for
    every kind, not only IMAGE (`ai/services/pipeline.py::create_generation`
    calls `ensure_generation_ready` unconditionally whenever a product is
    attached)."""
    if not case.with_product:
        return None

    name = f"Eval product ({case.name})"
    product = Product.objects.filter(workspace=workspace, name=name).first()
    if product is None:
        product = create_product(workspace=workspace, name=name, restrictions=case.restrictions)
    if not product.is_generation_ready:
        attach_reference_images(product=product, uploads=[_reference_upload()])
    return product


def run_benchmark() -> EvalResult:
    user, workspace = _benchmark_workspace()
    results: list[EvalCaseResult] = []

    for case in BENCHMARK_CASES:
        product = _benchmark_product(workspace, case)

        generation = create_generation(
            workspace=workspace,
            user=user,
            kind=case.kind,
            mode=case.mode,
            prompt=case.prompt,
            product=product,
        )
        run_generation(generation, n=1)
        generation.refresh_from_db()

        attempts = generation.quality_checks.count()
        identity_score = (
            generation.quality_checks.exclude(identity_score__isnull=True)
            .values_list("identity_score", flat=True)
            .first()
        )
        results.append(
            EvalCaseResult(
                name=case.name,
                passed=generation.status == GenerationStatus.SUCCEEDED,
                attempts=attempts,
                identity_score=identity_score,
                credits_charged=generation.credits_charged,
            )
        )

    return EvalResult(cases=results)
