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

TOTAL_STEPS = 8

#: Import (2) and Review (3) — the website import (steps-plan S1). Shown only
#: when `BRAND_IMPORT_S1` is on; off, the wizard is the original six screens.
IMPORT_STEPS: frozenset[int] = frozenset({2, 3})

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
ORG_SCOPE_STEPS: frozenset[int] = frozenset({1, 7})
WORKSPACE_SCOPE_STEPS: frozenset[int] = frozenset({2, 3, 4, 5, 6})

# design.md §10.4. What makes a step *done* — deliberately only the one thing
# each step exists to collect, not every field on it.
#
# Requiring the optional fields too (website, target audience) made pressing
# Continue advance the user while `current_step` stayed put, so resuming sent
# them backwards.
#
# Steps 1, 7 and 8 are absent because no workspace field decides them: they are
# satisfied by email verification, plan assignment and the flag itself. Steps 2
# and 3 are decided by the brand import (`_import_steps_done`).
STEP_COMPLETION_FIELD: dict[int, str] = {
    # Not `name`: registration always derives a placeholder from the email, so
    # keying on it would mark the Brand step done before the user saw it.
    # `description` is the one thing here only the user can supply — and it is
    # what grounds every later generation.
    4: "description",
    5: "category",
    # timezone always carries a default, so it cannot signal engagement;
    # choosing where to post can only have come from the user.
    6: "platforms",
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


def _imports_enabled(workspace: Workspace) -> bool:
    from billing.services.flags import BRAND_IMPORT_S1, flag_enabled

    return flag_enabled(workspace.organization, BRAND_IMPORT_S1)


def _import_steps_done(workspace: Workspace) -> list[int]:
    """Import is answered by a reading that finished (or a skip); Review by
    an applied import (or a skip). A failed reading answers neither."""
    from brand.models import BrandImport, ImportStatus

    statuses = set(BrandImport.objects.filter(workspace=workspace).values_list("status", flat=True))
    done = []
    if statuses & {ImportStatus.SUCCEEDED, ImportStatus.APPLIED, ImportStatus.SKIPPED}:
        done.append(2)
    if statuses & {ImportStatus.APPLIED, ImportStatus.SKIPPED}:
        done.append(3)
    return done


def completed_steps(workspace: Workspace, user: User) -> list[int]:
    done: list[int] = []
    if user.is_email_verified:
        done.append(1)
    if _imports_enabled(workspace):
        done.extend(_import_steps_done(workspace))
    done.extend(
        step for step, field in STEP_COMPLETION_FIELD.items() if is_filled(workspace, field)
    )
    if workspace.organization.plan_id:
        done.append(7)
    # A later brand inherits the org's answers rather than being asked again
    # (P0-58). Marking them done rather than hiding them keeps the rail honest:
    # they *are* satisfied, just not by this workspace.
    if is_shortened(workspace):
        done.extend(step for step in ORG_SCOPE_STEPS if step not in done)
    if workspace.onboarding_complete:
        done.append(8)
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
    return (
        Workspace.objects.filter(
            organization_id=workspace.organization_id, onboarding_complete=True
        )
        .exclude(pk=workspace.pk)
        .exists()
    )


def steps_for(workspace: Workspace) -> list[int]:
    """Which steps this workspace actually has to answer.

    The full run on a first brand; the brand steps plus the finish on every
    later one. Import and Review only while `BRAND_IMPORT_S1` is on.
    """
    hidden = frozenset() if _imports_enabled(workspace) else IMPORT_STEPS
    if not is_shortened(workspace):
        return [step for step in range(1, TOTAL_STEPS + 1) if step not in hidden]
    return sorted((WORKSPACE_SCOPE_STEPS - hidden) | {TOTAL_STEPS})


def current_step(workspace: Workspace, user: User) -> int:
    """The first step not yet satisfied — this is what makes it resumable.

    Walks `steps_for`, not `range(1, 9)`: a shortened run must not park the
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

        # **Seed a first taste profile** (P5-04). Inferred from whatever the
        # brand has already published, left *inactive* for the user to correct.
        # A blank profile at first run is the single largest predictor of early
        # churn, and asking somebody to describe their own voice before the
        # product has done anything for them is how that happens.
        #
        # Inside the `if` so a re-completion does not disturb a profile the
        # user has since written, and imported at call time because
        # `taste.services` reads `content.models`, which reads this app's
        # workspace in turn.
        from taste.services.seeding import seed_profile

        seed_profile(workspace=workspace, created_by=user)

    return workspace
