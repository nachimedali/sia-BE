"""Model routes: which model each task runs on, per package (pricing.plan.md Phase 4).

What is pinned here:

- resolution order — the package's row, then the default row, then settings;
- an empty table is the previous behaviour: every port is called with
  `model=None`, exactly as before the table existed;
- a row for another provider is skipped, never sent;
- every production call site names its task (a source scan, so a new call site
  cannot forget);
- the fallback runs once on a failure and never on a rate limit, and
  `Generation.model` records which one answered;
- the admin refuses a route that would never be used and records every edit.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest
from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory

from ai.admin import AIModelRouteAdmin
from ai.models import (
    AIModelRoute,
    AIModelRouteChange,
    AITask,
    Generation,
    GenerationKind,
    GenerationMode,
    GenerationStatus,
)
from ai.providers.fake import _fake_image_provider, _fake_text_provider, _fake_video_provider
from ai.providers.llm_text import get_text_provider
from ai.services.pipeline import create_generation, run_generation
from ai.services.routing import (
    SOURCE_DEFAULT,
    SOURCE_PLAN,
    SOURCE_SETTINGS,
    resolve,
    resolve_for,
)
from common.exceptions import ProviderError

pytestmark = pytest.mark.django_db


def _route(task: str, model: str, *, plan: Any = None, **extra: Any) -> AIModelRoute:
    from ai.models import kind_of
    from ai.services.routing import configured_provider

    return AIModelRoute.objects.create(
        task=task,
        plan=plan,
        provider=extra.pop("provider", configured_provider(kind_of(task))),
        model=model,
        **extra,
    )


# -----------------------------------------------------------------------------
# Resolution
# -----------------------------------------------------------------------------
def test_with_no_rows_the_settings_answer(plans) -> None:
    route = resolve(AITask.SENTIMENT, plan=plans["growth"])

    assert route.source == SOURCE_SETTINGS
    assert route.model == settings.LLM_DEFAULT_MODEL
    assert route.provider == settings.LLM_PROVIDER


def test_a_default_row_answers_every_package(plans) -> None:
    _route(AITask.SENTIMENT, "bulk-model")

    for code in ("spark", "essential", "growth", "scale"):
        route = resolve(AITask.SENTIMENT, plan=plans[code])
        assert (route.model, route.source) == ("bulk-model", SOURCE_DEFAULT), code
    assert resolve(AITask.SENTIMENT).model == "bulk-model"


def test_a_packages_row_beats_the_default_for_that_package_only(plans) -> None:
    _route(AITask.IMAGE_STUDIO, "image-default", params={"image_size": "2K"})
    _route(AITask.IMAGE_STUDIO, "image-spark", plan=plans["spark"], params={"image_size": "1K"})

    spark = resolve(AITask.IMAGE_STUDIO, plan=plans["spark"])
    growth = resolve(AITask.IMAGE_STUDIO, plan=plans["growth"])

    assert (spark.model, spark.params, spark.source) == (
        "image-spark",
        {"image_size": "1K"},
        SOURCE_PLAN,
    )
    assert (growth.model, growth.params) == ("image-default", {"image_size": "2K"})


def test_an_inactive_row_falls_through(plans) -> None:
    _route(AITask.DIGEST, "default-digest")
    _route(AITask.DIGEST, "scale-digest", plan=plans["scale"], is_active=False)

    assert resolve(AITask.DIGEST, plan=plans["scale"]).model == "default-digest"


def test_a_row_for_another_provider_is_never_sent(plans) -> None:
    """A model id belongs to one vendor: after a gateway swap the old rows are
    skipped and the task runs on settings until rows for the new one exist."""
    _route(AITask.DIGEST, "zernio-era-model", provider="some-other-gateway")

    route = resolve(AITask.DIGEST, plan=plans["growth"])

    assert route.source == SOURCE_SETTINGS
    assert route.model == settings.LLM_DEFAULT_MODEL


def test_a_workspace_resolves_through_its_organizations_plan(workspace, plans) -> None:
    _route(AITask.TOOLS, "default-tools")
    _route(AITask.TOOLS, "growth-tools", plan=plans["growth"])

    assert resolve_for(AITask.TOOLS, workspace).model == "default-tools"
    workspace.organization.plan = plans["growth"]
    workspace.organization.save(update_fields=["plan"])
    assert resolve_for(AITask.TOOLS, workspace).model == "growth-tools"
    assert resolve_for(AITask.TOOLS, None).model == "default-tools"


# -----------------------------------------------------------------------------
# An empty table is the previous behaviour
# -----------------------------------------------------------------------------
def test_an_empty_table_calls_every_port_exactly_as_before(
    workspace, user, generation_costs
) -> None:
    generation = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="a cosy morning",
    )
    run_generation(generation, n=1)

    assert _fake_text_provider.calls
    assert all(call["model"] is None for call in _fake_text_provider.calls)
    generation.refresh_from_db()
    assert generation.model == "fake-text-1"


# -----------------------------------------------------------------------------
# Every call site names its task
# -----------------------------------------------------------------------------
_GETTERS = {"get_text_provider", "get_image_provider"}
_SKIP_PARTS = {".venv", "tests", "migrations", "node_modules"}


def _production_modules() -> list[pathlib.Path]:
    root = pathlib.Path(settings.BASE_DIR)
    return [
        path
        for path in root.rglob("*.py")
        if not _SKIP_PARTS & set(path.relative_to(root).parts)
        and path.name not in {"llm_text.py", "nanobanana_image.py", "routed.py"}
    ]


def test_no_production_call_reaches_a_model_without_naming_its_task() -> None:
    """A bare `get_text_provider()` would run on settings forever, invisible to
    the route table. The getters take the task as their first argument."""
    offenders = []
    for path in _production_modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _GETTERS
                and not node.args
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"Name the AITask at: {', '.join(offenders)}"


def test_the_video_render_is_bound_to_the_workspaces_routes() -> None:
    source = (pathlib.Path(settings.BASE_DIR) / "ai" / "services" / "video.py").read_text()
    assert "get_video_provider(workspace=render.workspace)" in source


# -----------------------------------------------------------------------------
# Call sites receive the routed model
# -----------------------------------------------------------------------------
def test_studio_text_runs_on_its_route_and_records_it(workspace, user, generation_costs) -> None:
    _route(AITask.STUDIO_TEXT, "writer-1")

    generation = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="a cosy morning",
    )
    run_generation(generation, n=1)

    generation.refresh_from_db()
    assert generation.status == GenerationStatus.SUCCEEDED
    assert _fake_text_provider.calls[-1]["model"] == "writer-1"
    assert generation.model == "writer-1"


def test_an_image_generation_routes_image_check_and_copy_separately(
    workspace, user, product_with_reference_image, generation_costs, plans
) -> None:
    product = product_with_reference_image
    product.restrictions = ["no alcohol"]
    product.save(update_fields=["restrictions"])
    _route(AITask.IMAGE_STUDIO, "painter-1", params={"image_size": "2K"})
    _route(AITask.IMAGE_STUDIO, "painter-spark", plan=plans["spark"], params={"image_size": "1K"})
    _route(AITask.CONSTRAINT_CHECK, "inspector-1")
    _route(AITask.IMAGE_COPY, "copywriter-1")

    generation = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.IMAGE,
        mode=GenerationMode.PRODUCT,
        prompt="the mug on a sunlit table",
        product=product,
    )
    run_generation(generation, n=1)

    generation.refresh_from_db()
    assert generation.status == GenerationStatus.SUCCEEDED, generation.error_detail
    image_call = _fake_image_provider.calls[-1]
    # The workspace is on Spark, so Spark's own row answers.
    assert (image_call["model"], image_call["image_size"]) == ("painter-spark", "1K")
    assert set(_fake_text_provider.constraint_models) == {"inspector-1"}
    assert generation.model == "painter-spark"

    # Copy is written only when the brief asks for it; its call is routed on
    # its own task either way.
    from ai.services.pipeline import _write_copy

    _write_copy(generation, count=1)
    assert _fake_text_provider.calls[-1]["model"] == "copywriter-1"


def test_bulk_work_without_a_workspace_runs_on_the_default_row() -> None:
    from analytics.services.sentiment import classify_comments

    _route(AITask.SENTIMENT, "bulk-1")

    classify_comments(["I love this mug, the glaze is beautiful and it keeps coffee warm."])

    assert _fake_text_provider.calls[-1]["model"] == "bulk-1"


def test_a_clip_animates_on_the_route_for_its_role(workspace, plans) -> None:
    from ai.providers.video import get_video_provider

    _route(AITask.VIDEO_START, "first-frame-model", params={"resolution": "1080p"})
    _route(
        AITask.VIDEO_START,
        "first-frame-essential",
        plan=plans["essential"],
        params={"resolution": "720p"},
    )
    _route(AITask.VIDEO_END, "last-frame-model", params={"resolution": "1080p", "audio": True})
    workspace.organization.plan = plans["essential"]
    workspace.organization.save(update_fields=["plan"])

    provider = get_video_provider(workspace=workspace)
    assert provider is not None
    start = provider.animate(
        image=b"x", role="start", prompt="p", aspect="9:16", duration_seconds=5
    )
    provider.animate(image=b"x", role="end", prompt="p", aspect="9:16", duration_seconds=5)

    assert _fake_video_provider.requests[-2] == {
        "role": "start",
        "model": "first-frame-essential",
        "resolution": "720p",
        "audio": None,
    }
    assert _fake_video_provider.requests[-1] == {
        "role": "end",
        "model": "last-frame-model",
        "resolution": "1080p",
        "audio": True,
    }
    assert start.model == "first-frame-essential"


def test_a_route_can_switch_autopilots_batch_mode(workspace, plans) -> None:
    from ai.providers.nanobanana_image import get_image_provider

    _route(AITask.IMAGE_AUTOPILOT, "autopilot-painter", params={"batch": False})

    get_image_provider(AITask.IMAGE_AUTOPILOT, workspace=workspace).generate(
        prompt="p", reference_images=[], aspect="1:1", n=1, batch=True
    )

    assert _fake_image_provider.calls[-1]["batch"] is False


# -----------------------------------------------------------------------------
# Fallback
# -----------------------------------------------------------------------------
class _Flaky:
    """Refuses one model, answers on any other."""

    def __init__(self, refused: str, *, status: int = 404) -> None:
        self.refused = refused
        self.status = status
        self.models: list[str | None] = []

    def generate(self, *, system: str, prompt: str, n: int, model: str | None = None) -> Any:
        self.models.append(model)
        if model == self.refused:
            raise ProviderError("withdrawn", detail={"status": self.status})
        return _fake_text_provider.generate(system=system, prompt=prompt, n=n, model=model)


def test_the_fallback_answers_once_when_the_primary_fails(monkeypatch) -> None:
    flaky = _Flaky("gone-model")
    monkeypatch.setattr("ai.providers.llm_text.LLMTextProvider", lambda: flaky)
    monkeypatch.setattr(settings, "USE_FAKE_AI_PROVIDERS", False)
    _route(AITask.DIGEST, "gone-model", fallback_model="spare-model")

    result = get_text_provider(AITask.DIGEST).generate(system="s", prompt="p", n=1)

    assert flaky.models == ["gone-model", "spare-model"]
    assert result.model == "spare-model"


def test_a_rate_limit_is_never_retried_on_the_fallback(monkeypatch) -> None:
    """Same gateway, same account: a 429 on one model is a 429 on the other."""
    flaky = _Flaky("busy-model", status=429)
    monkeypatch.setattr("ai.providers.llm_text.LLMTextProvider", lambda: flaky)
    monkeypatch.setattr(settings, "USE_FAKE_AI_PROVIDERS", False)
    _route(AITask.DIGEST, "busy-model", fallback_model="spare-model")

    with pytest.raises(ProviderError):
        get_text_provider(AITask.DIGEST).generate(system="s", prompt="p", n=1)
    assert flaky.models == ["busy-model"]


def test_without_a_fallback_the_failure_surfaces(monkeypatch) -> None:
    flaky = _Flaky("gone-model")
    monkeypatch.setattr("ai.providers.llm_text.LLMTextProvider", lambda: flaky)
    monkeypatch.setattr(settings, "USE_FAKE_AI_PROVIDERS", False)
    _route(AITask.DIGEST, "gone-model")

    with pytest.raises(ProviderError):
        get_text_provider(AITask.DIGEST).generate(system="s", prompt="p", n=1)
    assert flaky.models == ["gone-model"]


def test_a_caller_naming_its_own_model_is_obeyed() -> None:
    _route(AITask.DIGEST, "routed-model", fallback_model="spare-model")

    get_text_provider(AITask.DIGEST).generate(system="s", prompt="p", n=1, model="eval-model")

    assert _fake_text_provider.calls[-1]["model"] == "eval-model"


def test_a_generation_records_the_fallback_that_answered(
    workspace, user, monkeypatch, generation_costs
) -> None:
    flaky = _Flaky("gone-model")
    monkeypatch.setattr("ai.providers.llm_text.LLMTextProvider", lambda: flaky)
    monkeypatch.setattr(settings, "USE_FAKE_AI_PROVIDERS", False)
    monkeypatch.setattr(settings, "USE_FAKE_TEXT_PROVIDER", False)
    _route(AITask.STUDIO_TEXT, "gone-model", fallback_model="spare-model")

    generation = create_generation(
        workspace=workspace,
        user=user,
        kind=GenerationKind.TEXT,
        mode=GenerationMode.IDEA,
        prompt="a cosy morning",
    )
    run_generation(generation, n=1)

    generation.refresh_from_db()
    assert generation.model == "spare-model"
    assert Generation.objects.filter(model="gone-model").count() == 0


# -----------------------------------------------------------------------------
# The admin
# -----------------------------------------------------------------------------
def _form_errors(**fields: Any) -> dict[str, list[str]]:
    route = AIModelRoute(**fields)
    with pytest.raises(ValidationError) as caught:
        route.full_clean()
    return caught.value.message_dict


def test_a_provider_the_port_does_not_point_at_is_refused() -> None:
    errors = _form_errors(task=AITask.DIGEST, provider="openrouter", model="anthropic/x")
    assert "provider" in errors


def test_a_parameter_the_kind_does_not_take_is_refused() -> None:
    errors = _form_errors(
        task=AITask.DIGEST, provider=settings.LLM_PROVIDER, model="m", params={"image_size": "2K"}
    )
    assert "params" in errors


def test_a_parameter_value_outside_its_set_is_refused() -> None:
    errors = _form_errors(
        task=AITask.IMAGE_STUDIO,
        provider=settings.IMAGE_PROVIDER,
        model="m",
        params={"image_size": "8K"},
    )
    assert "params" in errors
    errors = _form_errors(
        task=AITask.VIDEO_START,
        provider=settings.VIDEO_PROVIDER,
        model="m",
        params={"resolution": "4K"},
    )
    assert "params" in errors


def test_a_fallback_equal_to_the_primary_is_refused() -> None:
    errors = _form_errors(
        task=AITask.DIGEST, provider=settings.LLM_PROVIDER, model="m", fallback_model="m"
    )
    assert "fallback_model" in errors


def test_one_row_per_task_and_package() -> None:
    from django.db import IntegrityError, transaction

    _route(AITask.DIGEST, "a")
    with pytest.raises(IntegrityError), transaction.atomic():
        _route(AITask.DIGEST, "b")


def _admin_save(route: AIModelRoute, admin_user: Any, *, change: bool) -> None:
    request = RequestFactory().post("/")
    request.user = admin_user
    AIModelRouteAdmin(AIModelRoute, AdminSite()).save_model(
        request, route, form=None, change=change
    )


def test_every_admin_save_is_recorded_with_its_before_and_after(user) -> None:
    route = AIModelRoute(task=AITask.DIGEST, provider=settings.LLM_PROVIDER, model="first")
    _admin_save(route, user, change=False)
    route.model = "second"
    route.fallback_model = "spare"
    _admin_save(route, user, change=True)

    changes = list(AIModelRouteChange.objects.filter(route=route).order_by("created_at"))
    assert [c.before.get("model") for c in changes] == [None, "first"]
    assert [c.after["model"] for c in changes] == ["first", "second"]
    assert changes[-1].actor == user
    route.refresh_from_db()
    assert route.updated_by == user


def test_a_route_with_history_is_deactivated_not_deleted(user) -> None:
    route = AIModelRoute(task=AITask.DIGEST, provider=settings.LLM_PROVIDER, model="first")
    _admin_save(route, user, change=False)
    admin = AIModelRouteAdmin(AIModelRoute, AdminSite())

    assert admin.has_delete_permission(RequestFactory().get("/"), route) is False
    fresh = _route(AITask.TOOLS, "never-edited")
    assert admin.has_delete_permission(RequestFactory().get("/"), fresh) is True


def test_the_admin_says_which_cost_row_a_route_is_charged_at(generation_costs) -> None:
    admin = AIModelRouteAdmin(AIModelRoute, AdminSite())
    route = _route(AITask.IMAGE_STUDIO, "gemini-nano-banana-2.1")
    video = _route(AITask.VIDEO_START, "x-ai/grok-imagine-video-1.5-lite")

    assert "generic IMAGE" in admin.charged_at(route)
    assert "length rows" in admin.charged_at(video)


# -----------------------------------------------------------------------------
# The seed
# -----------------------------------------------------------------------------
def test_the_seed_routes_every_task_and_the_package_overrides(plans) -> None:
    from ai.route_seed import ROUTES

    call_command("seed_model_routes", verbosity=0)

    defaults = set(AIModelRoute.objects.filter(plan__isnull=True).values_list("task", flat=True))
    assert defaults == {task.value for task in AITask}
    assert AIModelRoute.objects.count() == len(ROUTES)
    spark = resolve(AITask.IMAGE_STUDIO, plan=plans["spark"])
    assert (spark.model, spark.params) == ("gemini-nano-banana-2.1", {"image_size": "1K"})
    essential = resolve(AITask.VIDEO_START, plan=plans["essential"])
    assert (essential.model, essential.params) == (
        "x-ai/grok-imagine-video-1.5-lite",
        {"resolution": "720p"},
    )
    # Every seeded row passes the admin's own validation on this deployment.
    for route in AIModelRoute.objects.all():
        route.full_clean()


def test_the_seed_never_overwrites_an_edit_unless_asked(plans) -> None:
    call_command("seed_model_routes", verbosity=0)
    AIModelRoute.objects.filter(task=AITask.DIGEST).update(model="operator-choice")

    call_command("seed_model_routes", verbosity=0)
    assert AIModelRoute.objects.get(task=AITask.DIGEST).model == "operator-choice"

    call_command("seed_model_routes", "--refresh", verbosity=0)
    route = AIModelRoute.objects.get(task=AITask.DIGEST)
    assert route.model == "meta/muse-glimmer-30b"
    change = AIModelRouteChange.objects.get(route=route)
    assert (change.before["model"], change.after["model"]) == (
        "operator-choice",
        "meta/muse-glimmer-30b",
    )


def test_the_migration_seeds_the_same_rows(plans, settings) -> None:
    from importlib import import_module

    from django.apps import apps

    settings.SEED_CATALOG_ON_MIGRATE = True
    seed = import_module("ai.migrations.0021_seed_model_routes")
    seed.forwards(apps, None)
    seed.forwards(apps, None)

    from ai.route_seed import ROUTES

    assert AIModelRoute.objects.count() == len(ROUTES)
