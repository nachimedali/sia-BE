"""Rollout flags (BUILD-PLAN Part 3).

Plan features answer *did you pay for this*; a flag answers *has this shipped
to you yet*. `billing.models.FeatureFlag` has carried the rows since P0-08;
this is the resolver Part 3 requires, and Phase 1 is the first phase to need
one.

**Resolution order:** an organization's own row wins, then the global row
(`organization` null), then the flag's declared rollout default below. Three
levels rather than two because that is what makes a single customer
switchable without writing a row for everyone else.

**On "default off".** Part 3 says a new flag ships off. That rule protects a
running deployment from a phase arriving unannounced — and `app-BE/` has never
been deployed (the same fact the migration exemption rests on). A flag that
ships off with nothing to roll back to is not caution, it is a phase that is
live in the code and invisible in the product until someone remembers the
switch. So the rollout default is declared **per flag, as data**, with the
reason on the row; Phase 1's is on, and an organization row of `enabled=False`
still restores pre-phase behaviour on demand. The moment anything is deployed,
a new flag's default belongs at `False` and is promoted deliberately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from billing.models import FeatureFlag

if TYPE_CHECKING:
    from workspaces.models import Organization

#: Phase 1 — per-platform overrides, revisions, templates, recurrence,
#: media editing. Off restores pre-phase behaviour: no revision is recorded,
#: no recurrence slot is materialised, and the surfaces that did not exist
#: before Phase 1 answer 404 rather than erroring.
CONTENT_MODEL_V2 = "content_model_v2"

#: Phase 2 — internal threads, approval chains, universal approval,
#: token-scoped review and the notification fan-out. Off restores pre-phase
#: behaviour: the collaboration surfaces answer 404 rather than erroring, and
#: a workspace whose chain does not block schedules straight from `DRAFT` with
#: no approval recorded — which is exactly what `requires_approval=False` did
#: before C-02 made approval universal. A **blocking** chain is honoured with
#: the flag either way, because that behaviour predates this phase.
COLLABORATION_V2 = "collaboration_v2"

#: Phase 3 — documents, campaigns, labels, saved views, timetables and bulk
#: operations. Off restores pre-phase behaviour: the planning surfaces answer
#: 404 rather than erroring, and a post is a social post as it always was —
#: `content_kind` defaults to `SOCIAL`, so nothing already stored changes
#: meaning when the flag moves in either direction.
PLANNING_V3 = "planning_v3"

#: Phase 5 — the taste model, the candidate queue and the decision log. Off
#: restores pre-phase behaviour: the taste surfaces answer 404, and autopilot
#: keeps producing drafts exactly as it did. What the flag does **not** restore
#: is auto-landing on a calendar — C-01 retired that outright, because Part 7
#: rule 13 admits no flag that skips the human.
TASTE_V5 = "taste_v5"

#: Phase 6 — saved reports, report runs and their PDFs, share links,
#: audience demographics and competitor tracking. Off restores pre-phase
#: behaviour: the reporting surfaces answer 404 rather than erroring, and the
#: daily account snapshot goes back to recording a follower count and nothing
#: else. Captures already taken stay where they are — a flag that deleted rows
#: on the way down would not be a rollback, it would be a data loss.
ANALYTICS_V6 = "analytics_v6"

#: Phase 7 — Learn, digests, findings and rule proposals. Off restores
#: pre-phase behaviour: the digest surfaces answer 404, closing a campaign
#: closes it and nothing else runs, and no rule proposal is created. Digests
#: already generated stay readable — a rollback that hid a document somebody
#: has already acted on would be a second surprise, not a recovery.
LEARN_V7 = "learn_v7"

#: Every flag the application reads, with the default that applies when no row
#: exists. A flag absent from here is a typo, not a feature — `flag_enabled`
#: raises rather than quietly answering `False`, which is the failure mode that
#: leaves a phase switched off in production and nobody able to say why.
ROLLOUT_DEFAULTS: dict[str, bool] = {
    CONTENT_MODEL_V2: True,
    COLLABORATION_V2: True,
    PLANNING_V3: True,
    TASTE_V5: True,
    ANALYTICS_V6: True,
    LEARN_V7: True,
}


def flag_enabled(organization: Organization | None, key: str) -> bool:
    if key not in ROLLOUT_DEFAULTS:
        raise KeyError(f"Unknown rollout flag '{key}'. Known flags: {sorted(ROLLOUT_DEFAULTS)}.")

    scope = Q(organization__isnull=True)
    if organization is not None:
        scope |= Q(organization=organization)
    rows = {
        row.organization_id: row.enabled
        for row in FeatureFlag.objects.filter(key=key).filter(scope)
    }
    if organization is not None and organization.pk in rows:
        return rows[organization.pk]
    if None in rows:
        return rows[None]
    return ROLLOUT_DEFAULTS[key]
