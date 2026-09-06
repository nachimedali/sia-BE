"""Gives every pre-migration workspace an organization (P0-52).

**Idempotent, batched and resumable**, in that order of importance:

* *idempotent* — a workspace that already has an organization is skipped, so a
  half-finished run can simply be started again;
* *batched* — a cursor over primary keys rather than one enormous transaction,
  because a migration that must complete in one shot is a migration that
  cannot be interrupted;
* *resumable* — the cursor is the last workspace id processed, printed on every
  batch, so an operator who has to stop can pick up where it stopped without
  re-reading what is already done.

One organization per existing workspace, named after it, with the owner, plan,
trial and Stripe customer copied up. That mapping is the only one that can be
right without asking anybody: before this migration a workspace *was* the paying
entity, so it becomes its own company. Merging several into one group is a
decision only the customer can make.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from workspaces.models import (
    Membership,
    Organization,
    OrganizationMembership,
    Role,
    Workspace,
    permissions_for,
)

BATCH = 200


class Command(BaseCommand):
    help = "Creates an Organization for every workspace that does not have one."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--after",
            type=int,
            default=0,
            help="Resume after this workspace id (printed by the previous run).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing anything.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        cursor = int(options["after"])
        dry_run = bool(options["dry_run"])
        created = 0
        filled = 0

        while True:
            batch = list(
                Workspace.objects.filter(pk__gt=cursor)
                .order_by("pk")
                .select_related("owner", "plan")[:BATCH]
            )
            if not batch:
                break

            for workspace in batch:
                cursor = workspace.pk
                if workspace.organization_id is not None:
                    continue
                if dry_run:
                    created += 1
                    continue
                self._provision(workspace)
                created += 1

            filled += self._fill_permissions(batch, dry_run=dry_run)
            self.stdout.write(f"  …through workspace {cursor} (organizations: {created})")

        verb = "would create" if dry_run else "created"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {created} organizations; backfilled {filled} permission sets."
            )
        )

    @transaction.atomic
    def _provision(self, workspace: Workspace) -> None:
        organization = Organization.objects.create(
            name=workspace.name,
            slug=Organization.unique_slug(workspace.name),
            owner=workspace.owner,
            plan=workspace.plan,
            trial_ends_at=workspace.trial_ends_at,
            stripe_customer_id=workspace.stripe_customer_id,
        )
        # Every existing member joins the company too. Omitting them would
        # leave collaborators visible in a brand and absent from the roster and
        # quota of the company above it.
        for membership in Membership.objects.filter(workspace=workspace).select_related("user"):
            OrganizationMembership.objects.get_or_create(
                organization=organization,
                user=membership.user,
                # OWNER is not assignable on a membership (P0-12); ownership is
                # the FK above, and ADMIN carries the same permissions.
                defaults={"role": Role.ADMIN if membership.role == Role.OWNER else membership.role},
            )
        Workspace.objects.filter(pk=workspace.pk).update(organization=organization)

    @staticmethod
    def _fill_permissions(batch: list[Workspace], *, dry_run: bool) -> int:
        """Derives `permissions` from `role` for rows written before the expand
        migration.

        Derivation is a pure function with an exhaustive 5x7 test behind it
        (`test_authority.py`), which is the only reason this is safe to run
        unattended: a migration that silently *widens* access is the worst
        outcome available here.
        """
        rows = Membership.objects.filter(workspace__in=batch, permissions=[])
        if dry_run:
            return int(rows.count())

        touched = 0
        for membership in rows:
            Membership.objects.filter(pk=membership.pk).update(
                permissions=sorted(permissions_for(membership.role))
            )
            touched += 1
        return touched
