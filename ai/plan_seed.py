"""A campaign plan's price lines (steps-plan S5).

Same contract as `ai/video_seed.py`: data, not behaviour. Each row is one line
of the estimate a person confirms before a plan is written, and its price is
`metadata.credits` — read by `planning.services.plans.quote` and nowhere else
(Part 7 rule 10). `ai/migrations/0014_plan_prices` seeds them with
`get_or_create`, so an edited price is never overwritten.
"""

from __future__ import annotations

from typing import Any

from ai.creative_seed import SEED_VERSION


def _row(key: str, label: str, credits: int) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "description": "",
        "icon_paths": [],
        "colors": [],
        "prompt_fragment": "",
        "metadata": {"credits": credits, "seed": SEED_VERSION},
    }


PLAN_CATALOG: dict[str, list[dict[str, Any]]] = {
    "plan_price": [
        _row("brief", "Brief and Brand Core reading", 2),
        _row("messages", "Three core messages with evidence", 3),
        _row("calendar", "Channel plan and dated calendar", 3),
    ]
}
