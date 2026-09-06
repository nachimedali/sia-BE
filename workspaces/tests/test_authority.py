"""The permission set and its five seeded presets (BUILD-PLAN Phase 0, P0-10..P0-13).

`Role` becomes display; `Membership.permissions` becomes authority. The two must
agree exactly during dual-write, and the derivation is a pure function so this
file can assert the whole 5x7 table rather than sampling it — "a migration that
silently widens access is the worst outcome available here".
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from workspaces.models import PERMISSIONS, Permission, Role, permissions_for

#: The authority table, written out rather than computed, so a change to
#: `permissions_for` has to be a deliberate edit *here* to pass. Derived from
#: the gates that existed before the permission set did:
#:   comment/edit  CONTRIBUTOR+  (`content/views.py`: "VIEWER cannot comment")
#:   approve/admin ADMIN+        (`content/views.py`, `workspaces/views.py`)
#:   view/analyze  every member  (no role gate on reads or on analytics)
#:   publish       EDITOR+       DELIBERATE NARROWING - see the module note in
#:                               `workspaces.models`; today `POST /posts/{id}/
#:                               schedule/` carries no role gate at all.
EXPECTED: dict[str, set[str]] = {
    Role.OWNER: {"view", "comment", "edit", "approve", "publish", "analyze", "admin"},
    Role.ADMIN: {"view", "comment", "edit", "approve", "publish", "analyze", "admin"},
    Role.EDITOR: {"view", "comment", "edit", "publish", "analyze"},
    Role.CONTRIBUTOR: {"view", "comment", "edit", "analyze"},
    Role.VIEWER: {"view", "analyze"},
}


def test_the_permission_set_is_exactly_the_seven_declared() -> None:
    assert frozenset(Permission.values) == PERMISSIONS
    assert {"view", "comment", "edit", "approve", "publish", "analyze", "admin"} == PERMISSIONS


@pytest.mark.parametrize("role", Role.values)
@pytest.mark.parametrize("permission", sorted(PERMISSIONS))
def test_every_role_permission_pair_is_pinned(role: str, permission: str) -> None:
    """5 roles x 7 permissions = 35 assertions, none of them sampled."""
    granted = permission in permissions_for(role)
    assert granted is (permission in EXPECTED[role]), (
        f"{role} x {permission}: derivation says {granted}, table says not"
    )


def test_every_role_is_covered_by_the_table() -> None:
    """A role added without a preset must fail here, not default to empty."""
    assert set(EXPECTED) == set(Role.values)


def test_presets_are_monotonic_down_the_rank() -> None:
    """Seniority is not decorative: a more senior role may never hold fewer
    permissions than a less senior one, or `ROLE_RANK` and the permission set
    would disagree about who outranks whom."""
    ordered = list(Role.values)  # OWNER -> VIEWER, most senior first
    for senior, junior in pairwise(ordered):
        assert permissions_for(junior) <= permissions_for(senior), (
            f"{junior} holds permissions {senior} does not"
        )


def test_permissions_for_rejects_an_unknown_role() -> None:
    """A typo'd role must not resolve to "no permissions" — that reads as a
    working deny and hides the bug until someone is locked out."""
    with pytest.raises(KeyError):
        permissions_for("SUPERUSER")


def test_derivation_returns_a_fresh_set_each_call() -> None:
    """The presets are module state; handing out the live object would let one
    caller's mutation re-grade every future membership."""
    first = permissions_for(Role.VIEWER)
    first.add("admin")
    assert "admin" not in permissions_for(Role.VIEWER)
