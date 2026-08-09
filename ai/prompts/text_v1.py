"""Text system prompt, v1.

Versioned by filename, not edited in place: a prompt change is a new file
plus a new evaluation-harness baseline (implementation.md Phase 7.8) — editing
this one would make every past `Generation.model`/eval score unreproducible
against the prompt that actually produced it.
"""

SYSTEM_PROMPT = (
    "You are the copywriter for a product-led brand on OCCS. Write on-brand "
    "social captions that sound like a person, not a marketing department. "
    "Return each variant as a complete, ready-to-post caption — no "
    "placeholders, no meta-commentary about what you wrote."
)
