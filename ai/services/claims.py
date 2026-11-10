"""Words an ad may not say on its own (S17-36).

Two lints the script writer's reply must pass before it is kept:

* `risky` — guarantees, absolutes, cures, "clinically", "risk-free", and lines
  that tell the viewer they have a trait (Meta's personal-attributes rule), in
  English, French and Italian (sales `legal-compliance-phrase-avoidance`,
  goose-skills `ad-policy-check`);
* `unproven` — a claim from the catalogue (organic, handmade, award-winning…)
  that the product has no proof on file for (goose-skills `claims-gate`).

A list, deliberately: deterministic and cheap, so it runs on every reply. It
narrows what the model may say; the pre-publish policy check (S4) still reads
every caption against the platforms' rules.
"""

from __future__ import annotations

import re

from ai.models import CreativeKind, CreativeOption
from products.models import Product

#: Patterns, matched case-insensitively on word boundaries.
_RISKY = [
    # guarantees
    r"guarantee[ds]?",
    r"garanti[es]?",
    r"garantit[oaie]?",
    r"risk[- ]free",
    r"sans risque",
    r"senza rischi",
    # absolutes and rankings
    r"the best",
    r"le meilleur",
    r"la meilleure",
    r"il migliore",
    r"la migliore",
    r"#\s?1",
    r"number one",
    r"num[ée]ro 1",
    r"numero uno",
    r"100\s?%",
    r"miracle",
    r"miracolo",
    # health and efficacy
    r"cures?",
    r"heals?",
    r"gu[ée]rit",
    r"guarisce",
    r"clinically",
    r"cliniquement",
    r"clinicamente",
    # telling the viewer they have a trait
    r"your (?:acne|wrinkles|weight|belly|hair loss|debt|anxiety|depression)",
    r"you(?:'re| are) (?:overweight|fat|old|ugly|broke)",
    r"(?:votre|vos|ton|tes) (?:acn[ée]|rides|poids|kilos|dettes)",
    r"(?:la tua|le tue|il tuo) (?:acne|rughe|peso|debiti)",
]
_RISKY_RE = re.compile(r"(?<![\w#])(?:" + "|".join(_RISKY) + r")(?!\w)", re.IGNORECASE)


def risky(text: str) -> list[str]:
    """The risky phrases found in `text`, as written there."""
    return [match.group(0) for match in _RISKY_RE.finditer(text or "")]


def unproven(text: str, product: Product) -> list[str]:
    """Catalogue claims `text` makes that the product has no proof on file for."""
    proven = {c["key"] for c in product.claims or [] if c.get("proof_media")}
    low = (text or "").lower()
    found = []
    for row in CreativeOption.objects.filter(kind=CreativeKind.CLAIM, is_active=True):
        if row.key in proven:
            continue
        phrases = [str(p).lower() for p in (row.metadata or {}).get("phrases") or [] if str(p)]
        if any(re.search(rf"(?<!\w){re.escape(p)}(?!\w)", low) for p in phrases):
            found.append(row.key)
    return found
