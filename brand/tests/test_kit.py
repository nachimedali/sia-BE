"""The brand kit reader (S1-06) and the places that read it: the Studio's image
prompt and the pre-publish palette check."""

from __future__ import annotations

from typing import Any

import pytest

from ai.services.prompting import assemble_image_prompt
from brand.models import BrandCore
from brand.services.kit import BrandKit, kit_for, prompt_lines

pytestmark = pytest.mark.django_db


def core(
    workspace: Any, colors: list[dict[str, str]], fonts: dict[str, Any] | None = None, **kw: Any
) -> BrandCore:
    return BrandCore.objects.create(
        workspace=workspace,
        version=kw.pop("version", 1),
        sections={"palette": {"value": {"colors": colors, "fonts": fonts or {}}, "origin": "site"}},
    )


def test_no_applied_import_means_no_kit(workspace: Any) -> None:
    assert kit_for(workspace) is None


def test_the_kit_holds_brand_colours_and_fonts_and_ignores_spare_swatches(workspace: Any) -> None:
    core(
        workspace,
        [
            {"role": "Primary", "hex": "#3f5a1e"},
            {"role": "Colour", "hex": "#ABCDEF"},
            {"role": "Accent", "hex": "#C2A03B"},
            {"role": "Highlight", "hex": "not-a-colour"},
        ],
        {"display": {"name": "Playfair Display"}, "body": {"name": ""}},
    )

    kit = kit_for(workspace)

    assert kit is not None
    assert kit.colors == (("Primary", "#3F5A1E"), ("Accent", "#C2A03B"))
    assert kit.fonts == {"display": "Playfair Display"}


def test_a_later_version_replaces_an_earlier_one(workspace: Any) -> None:
    core(workspace, [{"role": "Primary", "hex": "#111111"}])
    core(workspace, [{"role": "Primary", "hex": "#222222"}], version=2)

    kit = kit_for(workspace)
    assert kit is not None and kit.hexes == ["#222222"] and kit.version == 2


def test_an_import_that_found_no_brand_colour_is_no_kit(workspace: Any) -> None:
    core(workspace, [{"role": "Colour", "hex": "#ABCDEF"}])

    assert kit_for(workspace) is None


def test_another_workspaces_kit_is_not_read(workspace: Any, other_user: Any) -> None:
    from workspaces.services.provisioning import provision_workspace

    theirs = provision_workspace(other_user, name="Elsewhere")
    core(theirs, [{"role": "Primary", "hex": "#999999"}])

    assert kit_for(workspace) is None


def test_prompt_lines_name_the_colours_and_never_the_fonts() -> None:
    kit = BrandKit(1, (("Primary", "#3F5A1E"),), {"display": "Playfair Display"}, False)

    [line] = prompt_lines(kit)

    assert "primary #3F5A1E" in line and "Playfair" not in line
    assert prompt_lines(None) == []
    assert prompt_lines(BrandKit(1, (), {"display": "Lato"}, False)) == []


def test_the_studio_image_prompt_carries_the_kit_after_a_rewrite(workspace: Any) -> None:
    core(workspace, [{"role": "Primary", "hex": "#3F5A1E"}])

    plain = assemble_image_prompt(idea="olive oil on a table", workspace=workspace)
    rewritten = assemble_image_prompt(
        idea="olive oil on a table",
        workspace=workspace,
        rephrased="A tall bottle in morning light.",
    )

    for prompt in (plain, rewritten):
        assert "primary #3F5A1E" in prompt.user
        assert prompt.grounding["brand_kit"] is True


def test_without_a_kit_the_image_prompt_is_unchanged(workspace: Any) -> None:
    prompt = assemble_image_prompt(idea="olive oil on a table", workspace=workspace)

    assert "Brand colours" not in prompt.user
    assert "brand_kit" not in prompt.grounding
