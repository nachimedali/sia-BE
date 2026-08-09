"""Grounded prompt assembly (design.md §8.3).

design.md names three retrieved contexts: category signal (a trend cluster or
`CreativeRecipe`), voice, and performance (the workspace's own top-percentile
posts). Only voice is available in Phase 7 — category signal needs Phase 10's
`TrendCluster`/Phase 14's `CreativeRecipe`, and performance needs Phase 11's
percentile analytics. This is the documented, intentional gap implementation.md
§9's build-order rationale describes: "Trends (P10) after generation (P7), so
grounding is a measurable upgrade to a working generator rather than an
unfalsifiable bet." `GroundedPrompt.grounding` records which sources actually
fired, so Phase 10/11 slot into `_category_signal`/`_performance_signal` below
without changing this function's shape (design.md §15.8 A70).

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
    """Deferred to Phase 10 (`TrendCluster`) / Phase 14 (`CreativeRecipe`)."""
    return None


def _performance_signal(workspace: Workspace) -> str | None:
    """Deferred to Phase 11 (percentile analytics)."""
    return None


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
    grounding: dict[str, object] = {
        "voice": True,
        "restrictions": bool(product and product.restrictions),
        "category_signal": _category_signal(workspace) is not None,
        "performance": _performance_signal(workspace) is not None,
    }

    user_lines = [idea.strip()]
    if product is not None:
        user_lines.append(f"Product: {product.name}.")
        if product.description:
            user_lines.append(product.description)
    user_lines += _restriction_lines(product)

    signal = _category_signal(workspace)
    if signal:
        user_lines.append(signal)
    performance = _performance_signal(workspace)
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
    grounding: dict[str, object] = {
        "restrictions": bool(product and product.restrictions),
        "category_signal": _category_signal(workspace) is not None,
    }

    user_lines = [IMAGE_PROMPT_PREFIX, idea.strip()]
    if render_style:
        user_lines.append(f"Render style: {render_style}.")
    if scene:
        user_lines.append(f"Scene: {scene}.")
    user_lines += _restriction_lines(product)

    signal = _category_signal(workspace)
    if signal:
        user_lines.append(signal)

    return GroundedPrompt(system="", user="\n\n".join(user_lines), grounding=grounding)
