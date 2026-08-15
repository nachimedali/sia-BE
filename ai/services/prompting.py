"""Grounded prompt assembly (design.md §8.3).

design.md names three retrieved contexts: category signal (a trend cluster or
`CreativeRecipe`), voice, and performance (the workspace's own top-percentile
posts). **All three are live** as of Phase 11 — voice since Phase 7, category
signal since Phase 10 wired `_category_signal` to the real `TrendCluster`
corpus, and performance since Phase 11 wired `_performance_signal` to real
percentile analytics. That is the measurable upgrade implementation.md §9's
build-order rationale describes ("Trends (P10) after generation (P7), so
grounding is a measurable upgrade to a working generator rather than an
unfalsifiable bet"), and it closes A70 (design.md §15.8).

Every signal is **opportunistic and read-only**: a workspace with no corpus, no
history, or a plan whose analytics horizon is zero generates exactly as it did
in Phase 7 rather than failing. None of them may reach a provider — grounding
must never be able to make a generation slow or broken.

`GroundedPrompt.grounding` records which sources actually fired, so a caller —
or the eval harness — can assert that a prompt was grounded without re-deriving
it from the objects.

Product `restrictions` are injected as hard constraints, never as a
suggestion — the quality gate's brand-constraint check (design.md §8.3)
re-verifies them independently, so a prompt that ignores them is still caught.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ai.prompts.image_v1 import PROMPT_PREFIX as IMAGE_PROMPT_PREFIX
from ai.prompts.text_v1 import SYSTEM_PROMPT as TEXT_SYSTEM_PROMPT

if TYPE_CHECKING:
    from ai.models import VoiceProfile
    from products.models import Product
    from workspaces.models import Workspace


@dataclass(frozen=True)
class GroundedPrompt:
    system: str
    user: str
    # What actually fed the prompt — not the whole workspace/product, just the
    # facts, so a caller (or the eval harness) can assert on grounding without
    # re-deriving it from the objects.
    grounding: dict[str, object] = field(default_factory=dict)


def _category_signal(workspace: Workspace) -> str | None:
    """The strongest fresh trend cluster for this workspace's category.

    Read-only against the cache: `top_cluster` never triggers an extraction, so
    a generation cannot be made slow — or made to fail — by a trend vendor
    being down. No corpus yet means an ungrounded prompt, which is exactly what
    Phases 7-9 shipped, rather than an error.

    Descriptive, not prescriptive: the cluster's label and how many independent
    sources it was drawn from. That framing matters — the model is told what is
    *working* in the category, not handed something to copy, which is the same
    line design.md §8.4's synthesis rule draws for recipes.
    """
    from trends.services import top_cluster

    if workspace.category_id is None or not workspace.platforms:
        return None
    cluster = top_cluster(int(workspace.category_id), str(workspace.platforms[0]))
    if cluster is None:
        return None
    return (
        f"Currently working in this category on {cluster.get_platform_display()}: "
        f"{cluster.label} (observed across {cluster.item_count} independent posts). "
        "Take the approach, not the wording."
    )


def _performance_signal(workspace: Workspace) -> str | None:
    """The workspace's own top-percentile post, as a pattern to lean on.

    design.md §8.3's third retrieved context, live since Phase 11. Read-only
    against stored captures — like the category signal, this must never make a
    generation wait on, or fail because of, something outside the request.

    The *opening* is what gets quoted, not the whole post: what transfers
    between two posts by the same brand is how they start, and handing over a
    full caption invites the model to rewrite it rather than learn from it.
    """
    from analytics.services import signals
    from billing.services.entitlements import entitlements_for

    horizon = entitlements_for(workspace).analytics_horizon_days()
    if not horizon:
        return None

    rows = signals.performance(workspace.pk, horizon_days=horizon)
    best = next((row for row in rows if row.percentile is not None and row.percentile >= 80), None)
    if best is None:
        return None

    from content.models import Post

    body = Post.objects.filter(pk=best.post_id).values_list("master_body", flat=True).first()
    if not body:
        return None

    opening = " ".join(body.split()[:25])
    return (
        f"Your own best-performing post on this platform opened like this: "
        f'"{opening}". Match what makes it work, not its wording.'
    )


def _voice_lines(workspace: Workspace, voice_profile: VoiceProfile | None) -> list[str]:
    lines = [f"Brand voice: {workspace.get_brand_voice_default_display()}."]
    if voice_profile is not None:
        if voice_profile.system_prompt:
            lines.append(voice_profile.system_prompt)
        if voice_profile.tone_descriptors:
            lines.append("Tone: " + ", ".join(voice_profile.tone_descriptors) + ".")
        if voice_profile.banned_phrases:
            banned = ", ".join(f'"{p}"' for p in voice_profile.banned_phrases)
            lines.append(f"Never use these phrases: {banned}.")
    return lines


def _restriction_lines(product: Product | None) -> list[str]:
    if product is None or not product.restrictions:
        return []
    constraints = "\n".join(f"- {r}" for r in product.restrictions)
    return [f"Hard constraints — must not be violated:\n{constraints}"]


def assemble_text_prompt(
    *,
    idea: str,
    workspace: Workspace,
    product: Product | None = None,
    voice_profile: VoiceProfile | None = None,
) -> GroundedPrompt:
    # Resolved once each. Both are real queries — the performance signal walks
    # every capture in the plan's horizon — so computing them a second time to
    # fill in `grounding` would double the cost of assembling every prompt.
    signal = _category_signal(workspace)
    performance = _performance_signal(workspace)

    grounding: dict[str, object] = {
        "voice": True,
        "restrictions": bool(product and product.restrictions),
        "category_signal": signal is not None,
        "performance": performance is not None,
    }

    user_lines = [idea.strip()]
    if product is not None:
        user_lines.append(f"Product: {product.name}.")
        if product.description:
            user_lines.append(product.description)
    user_lines += _restriction_lines(product)

    if signal:
        user_lines.append(signal)
    if performance:
        user_lines.append(performance)

    system = "\n".join([TEXT_SYSTEM_PROMPT, *_voice_lines(workspace, voice_profile)])
    return GroundedPrompt(system=system, user="\n\n".join(user_lines), grounding=grounding)


def assemble_image_prompt(
    *,
    idea: str,
    workspace: Workspace,
    product: Product | None = None,
    render_style: str = "",
    scene: str = "",
) -> GroundedPrompt:
    signal = _category_signal(workspace)
    grounding: dict[str, object] = {
        "restrictions": bool(product and product.restrictions),
        "category_signal": signal is not None,
    }

    user_lines = [IMAGE_PROMPT_PREFIX, idea.strip()]
    if render_style:
        user_lines.append(f"Render style: {render_style}.")
    if scene:
        user_lines.append(f"Scene: {scene}.")
    user_lines += _restriction_lines(product)

    if signal:
        user_lines.append(signal)

    return GroundedPrompt(system="", user="\n\n".join(user_lines), grounding=grounding)
