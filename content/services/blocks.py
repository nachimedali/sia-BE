"""The structured block format a `DOC` post's body is written in (P3-02).

**Not HTML, and the distinction is load-bearing.** HTML in a database is a
sanitisation liability and an export dead end: every read becomes a trust
decision, the only safe renderer is one that has already parsed it, and the
day something needs the content in another shape — a PDF, a digest, a search
index — the parse has to happen again, differently, by whoever is writing that
feature. A declared block list is data: it can be rendered, indexed, diffed and
exported without anyone deciding whether to trust it.

**Declared as data, like `rules.py`.** A block type is a row in `BLOCK_SCHEMA`,
not a branch, so adding one is an entry plus a test rather than a new code
path — the same property that Phase 4 will lean on for platforms.

The format is deliberately **flat**: blocks do not nest. Nesting buys little
for a marketing memo and costs a recursive validator, a recursive renderer and
a depth limit that someone eventually forgets to enforce. Emphasis is carried
by `marks` — offset ranges over a block's own text, the same anchoring an
`Annotation` uses (P2-02) — rather than by nested inline nodes.
"""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import ValidationError

#: An unbounded document is a denial of service with extra steps: a hundred
#: thousand blocks is a slow validator, a slow render and a row nothing can
#: page through. The same reasoning caps recurrence expansion at 500 slots
#: (P1-10) — user input needs a bound, and a generous one is still a bound.
MAX_BLOCKS = 1_000

#: Per block type: which keys it takes, which are required, and what each must
#: be. `text` is spelled out per type rather than inherited, so a type that
#: should not carry prose (a divider) cannot quietly grow it.
BLOCK_SCHEMA: dict[str, dict[str, Any]] = {
    "heading": {
        "required": {"text": str, "level": int},
        "optional": {"marks": list},
        "choices": {"level": (1, 2, 3)},
    },
    "paragraph": {"required": {"text": str}, "optional": {"marks": list}},
    "quote": {"required": {"text": str}, "optional": {"marks": list}},
    "bullet_list": {"required": {"items": list}, "optional": {}},
    "ordered_list": {"required": {"items": list}, "optional": {}},
    "code": {"required": {"text": str}, "optional": {"language": str}},
    "divider": {"required": {}, "optional": {}},
    "image": {"required": {"media_asset_id": int}, "optional": {"alt_text": str}},
}

#: Inline emphasis. `link` is the only one carrying a payload, and the only one
#: that can express executable intent — hence `SAFE_LINK_SCHEMES`.
MARK_TYPES: dict[str, dict[str, Any]] = {
    "bold": {"required": {}},
    "italic": {"required": {}},
    "code": {"required": {}},
    "link": {"required": {"href": str}},
}

#: `javascript:`, `data:` and `vbscript:` are the ways a link becomes a script.
#: An allowlist rather than a denylist: a denylist is a list of the schemes
#: someone thought of, and browsers keep inventing more.
SAFE_LINK_SCHEMES = ("http://", "https://", "mailto:", "/")


def _fail(message: str, **detail: Any) -> None:
    raise ValidationError({"doc_body": message, **detail})


def _validate_marks(block: dict[str, Any], index: int) -> None:
    marks = block.get("marks")
    if marks is None:
        return
    if not isinstance(marks, list):
        _fail(f"Block {index}: 'marks' must be a list.")

    text = block.get("text")
    if not isinstance(text, str):
        # A mark is a range over text. On a block with none it is not merely
        # useless, it is unrenderable — and silently dropping it would lose
        # formatting the author applied.
        _fail(f"Block {index}: '{block['type']}' blocks carry no text to mark.")

    for position, mark in enumerate(marks):
        where = f"Block {index}, mark {position}"
        if not isinstance(mark, dict):
            _fail(f"{where}: each mark must be an object.")
        kind = mark.get("type")
        if kind not in MARK_TYPES:
            _fail(f"{where}: unknown mark type {kind!r}.", allowed=sorted(MARK_TYPES))

        spec = MARK_TYPES[kind]
        allowed = {"type", "start", "end", *spec["required"]}
        unknown = sorted(set(mark) - allowed)
        if unknown:
            _fail(f"{where}: unknown key(s) {', '.join(unknown)}.")
        for key, expected in spec["required"].items():
            if not isinstance(mark.get(key), expected):
                _fail(f"{where}: '{key}' is required and must be {expected.__name__}.")

        start, end = mark.get("start"), mark.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool):
            _fail(f"{where}: 'start' and 'end' must be integers.")
        # Half-open and non-empty, the same shape an `Annotation` anchor takes.
        # A zero-width mark formats nothing and can never be seen to be wrong.
        if not 0 <= start < end <= len(text):  # type: ignore[arg-type]
            _fail(
                f"{where}: [{start}, {end}) is not a range inside text of length {len(text)}."  # type: ignore[arg-type]
            )

        if kind == "link" and not mark["href"].startswith(SAFE_LINK_SCHEMES):
            _fail(f"{where}: only {', '.join(SAFE_LINK_SCHEMES)} links are allowed.")


def _validate_image(block: dict[str, Any], index: int, workspace: Any) -> None:
    """The one cross-tenant check in the format, and it **fails closed**.

    P1-11 found this exact hole in platform options: a media reference
    validated as an `int` accepts another tenant's asset id perfectly happily,
    because validating the *type* is not validating the *reference*. A caller
    that omits the workspace gets a refusal rather than a free pass.
    """
    from content.models import MediaAsset

    if workspace is None:
        _fail(f"Block {index}: an image block cannot be validated without a workspace.")
    if not MediaAsset.objects.filter(pk=block["media_asset_id"], workspace=workspace).exists():
        _fail(
            f"Block {index}: no such image in this workspace.",
            media_asset_id=block["media_asset_id"],
        )


def validate_document(document: Any, *, workspace: Any = None) -> list[dict[str, Any]]:
    """Returns the document unchanged, or raises `ValidationError`.

    Unchanged rather than normalised on purpose: a validator that rewrites is
    a validator whose output nobody can predict from its input, and the stored
    row would stop matching what the author sent.
    """
    if not isinstance(document, list):
        _fail("A document is a list of blocks.")
    if len(document) > MAX_BLOCKS:
        _fail(f"A document may hold at most {MAX_BLOCKS} blocks; this has {len(document)}.")

    for index, block in enumerate(document):
        if not isinstance(block, dict):
            _fail(f"Block {index}: each block must be an object.")
        kind = block.get("type")
        if kind not in BLOCK_SCHEMA:
            _fail(f"Block {index}: unknown block type {kind!r}.", allowed=sorted(BLOCK_SCHEMA))

        spec = BLOCK_SCHEMA[kind]
        allowed = {"type", *spec["required"], *spec["optional"]}
        unknown = sorted(set(block) - allowed)
        if unknown:
            _fail(f"Block {index}: unknown key(s) {', '.join(unknown)}.", allowed=sorted(allowed))

        for key, expected in {**spec["required"], **spec["optional"]}.items():
            if key not in block:
                if key in spec["required"]:
                    _fail(f"Block {index}: '{key}' is required.")
                continue
            # `bool` is an `int` in Python, and `level: true` should not pass.
            if isinstance(block[key], bool) is not (expected is bool) or not isinstance(
                block[key], expected
            ):
                _fail(f"Block {index}: '{key}' must be {expected.__name__}.")

        for key, choices in spec.get("choices", {}).items():
            if key in block and block[key] not in choices:
                _fail(f"Block {index}: '{key}' must be one of {choices}.")

        if kind in ("bullet_list", "ordered_list") and not all(
            isinstance(item, str) for item in block["items"]
        ):
            _fail(f"Block {index}: list items must be strings.")

        if kind == "image":
            _validate_image(block, index, workspace)

        _validate_marks(block, index)

    return list(document)
