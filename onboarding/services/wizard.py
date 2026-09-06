"""Wizard progress and completion (implementation.md Phase 2.4, §4.1).

The one authority on what "done" means. It was previously split — the serializer
decided which steps to render as complete, the view decided what `complete/`
would accept — and A27 exists because a mismatch between those two answers sent
resuming users backwards. One definition, two readers.
"""

from __future__ import annotations

from accounts.models import User
from common.exceptions import OCCSError
from workspaces.models import Workspace

TOTAL_STEPS = 6

# --- scope (BUILD-PLAN L-1, P0-58) -------------------------------------------
#
# The wizard collects two different kinds of thing, and the org migration made
# the difference matter. Email verification and plan selection belong to the
# **account and the company**: they are answered once, and answering them again
# for a second brand is a worse question than a redundant one — it implies the
# second brand might be on a different plan, which it cannot be.
#
# Brand, market and operating preferences belong to the **workspace**. Each new
# brand genuinely needs them, and each has different answers.
ORG_SCOPE_STEPS: frozenset[int] = frozenset({1, 5})
WORKSPACE_SCOPE_STEPS: frozenset[int] = frozenset({2, 3, 4})

# design.md §10.4. What makes a step *done* — deliberately only the one thing
# each step exists to collect, not every field on it.
#
# Requiring the optional fields too (website, target audience) made pressing
# Continue advance the user while `current_step` stayed put, so resuming sent
# them backwards.
#
# Steps 1, 5 and 6 are absent because no workspace field decides them: they are
# satisfied by email verification, plan assignment and the flag itself.
STEP_COMPLETION_FIELD: dict[int, str] = {
    # Not `name`: registration always derives a placeholder from the email, so
    # keying on it would mark the Brand step done before the user saw it.
    # `description` is the one thing here only the user can supply — and it is
    # what grounds every later generation.
    2: "description",
    3: "category",
    # timezone always carries a default, so it cannot signal engagement;
    # choosing where to post can only have come from the user.
    4: "platforms",
}

# What `complete/` insists on. Deliberately short: D14 says nobody hits a
# paywall or a wall of required fields before seeing the product.
REQUIRED_TO_COMPLETE: tuple[str, ...] = ("name", "category", "timezone")


def is_filled(workspace: Workspace, field: str) -> bool:
    value = (
        getattr(workspace, f"{field}_id", None)
        if field == "category"
        else getattr(workspace, field)
    )
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return value not in (None, "")


def completed_steps(workspace: Workspace, user: User) -> list[int]:
    done: list[int] = []
    if user.is_email_verified:
        done.append(1)
    done.extend(
        step for step, field in STEP_COMPLETION_FIELD.items() if is_filled(workspace, field)
    )
    if workspace.plan_id:
        done.append(5)
    # A later brand inherits the org's answers rather than being asked again
    # (P0-58). Marking them done rather than hiding them keeps the rail honest:
    # they *are* satisfied, just not by this workspace.
    if is_shortened(workspace):
        done.extend(step for step in ORG_SCOPE_STEPS if step not in done)
    if workspace.onboarding_complete:
        done.append(6)
    return sorted(done)


def is_shortened(workspace: Workspace) -> bool:
    """Whether this workspace re-runs the **brand steps only** (P0-58).

    True for the second and every later brand in an organization: the account
    is already verified and the company is already on a plan, so asking again
    is not merely redundant — it implies the new brand could be on a different
    plan, which the model does not allow (L-1: billing is per workspace,
    entitlement is per organization).

    Keyed on there being an *earlier* workspace rather than on a flag, so it
    cannot drift out of step with reality: delete the first brand and the next
    one legitimately becomes the first again.
    """
    organization_id = workspace.organization_id
    if organization_id is None:
        return False
    return (
        Workspace.objects.filter(organization_id=organization_id, onboarding_complete=True)
        .exclude(pk=workspace.pk)
        .exists()
    )


def steps_for(workspace: Workspace) -> list[int]:
    """Which steps this workspace actually has to answer.

    The full six on a first run; the three brand steps plus the finish on every
    later one.
    """
    if not is_shortened(workspace):
        return list(range(1, TOTAL_STEPS + 1))
    return sorted(WORKSPACE_SCOPE_STEPS | {TOTAL_STEPS})


def current_step(workspace: Workspace, user: User) -> int:
    """The first step not yet satisfied — this is what makes it resumable.

    Walks `steps_for`, not `range(1, 7)`: a shortened run must not park the
    user on a step it never intends to show them.
    """
    done = set(completed_steps(workspace, user))
    applicable = steps_for(workspace)
    return next((step for step in applicable if step not in done), applicable[-1])


def complete_onboarding(workspace: Workspace, user: User) -> Workspace:
    """Validates the required fields and flips `onboarding_complete`."""
    if not user.is_email_verified:
        raise OCCSError(
            "Confirm your email address before finishing setup.",
            code="email_not_verified",
        )

    missing = [field for field in REQUIRED_TO_COMPLETE if not is_filled(workspace, field)]
    if missing:
        raise OCCSError(
            "Some required details are still missing.",
            code="onboarding_incomplete",
            detail={"missing": missing},
        )

    if not workspace.onboarding_complete:
        workspace.onboarding_complete = True
        workspace.save(update_fields=["onboarding_complete", "updated_at"])

    return workspace
