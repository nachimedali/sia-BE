"""Backfill half of the contract step (P0-56).

Separate from the schema half (0011) because they cannot share a transaction:
inserting organizations leaves deferred foreign-key triggers pending, and
Postgres refuses to ALTER a table with pending trigger events. Two migrations
is also the more honest shape — the data lands, then the constraint that
depends on it is added.

Expand (0004) added `Workspace.organization` nullable and
`Membership.permissions` empty; dual-write kept both sides current; P0-55 cut
reads over to the organization. This drops what is no longer read.

**The backfill runs before the constraint.** It was a standalone command
during expand, when it had to be resumable across a live deploy. Once the
columns it reads are being dropped in this same migration, a separate command
that must have run first is a footgun: it would import fields that no longer
exist. Folding it in makes "the backfill ran" a property of the schema version
rather than of an operator's memory.

One organization per orphan workspace, named after it, owner/plan/trial/Stripe
copied up. That mapping is the only one that can be right without asking
anybody: before the migration a workspace *was* the paying entity, so it becomes
its own company. Merging several into one group is a decision only the customer
can make.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.db import migrations
from django.utils.text import slugify

BATCH = 200

#: Mirrors `workspaces.models._PRESETS`. Duplicated on purpose: a migration
#: reads the schema as it was, and importing the live module would make this
#: file's behaviour change every time the presets do.
PRESETS = {
    "OWNER": ["admin", "analyze", "approve", "comment", "edit", "publish", "view"],
    "ADMIN": ["admin", "analyze", "approve", "comment", "edit", "publish", "view"],
    "EDITOR": ["analyze", "comment", "edit", "publish", "view"],
    "CONTRIBUTOR": ["analyze", "comment", "edit", "view"],
    "VIEWER": ["analyze", "view"],
}


def _unique_slug(Organization, name):
    base = slugify(name) or "organization"
    slug = base
    while Organization.objects.filter(slug=slug).exists():
        slug = f"{base}-{secrets.token_hex(3)}"
    return slug


def backfill(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    Organization = apps.get_model("workspaces", "Organization")
    OrganizationMembership = apps.get_model("workspaces", "OrganizationMembership")
    Membership = apps.get_model("workspaces", "Membership")

    orphans = Workspace.objects.filter(organization__isnull=True).order_by("pk")
    for workspace in orphans.iterator(chunk_size=BATCH):
        organization = Organization.objects.create(
            name=workspace.name,
            slug=_unique_slug(Organization, workspace.name),
            owner_id=workspace.owner_id,
            plan_id=workspace.plan_id,
            trial_ends_at=workspace.trial_ends_at,
            stripe_customer_id=workspace.stripe_customer_id,
            referral_code=secrets.token_urlsafe(9),
        )
        # Every existing member joins the company too. Omitting them would
        # leave collaborators visible in a brand and absent from the roster and
        # quota of the company above it. OWNER is not assignable on a
        # membership (P0-12) — ownership is the FK above — and ADMIN carries
        # the same permission set.
        for membership in Membership.objects.filter(workspace=workspace):
            OrganizationMembership.objects.get_or_create(
                organization=organization,
                user_id=membership.user_id,
                defaults={"role": "ADMIN" if membership.role == "OWNER" else membership.role},
            )
        Workspace.objects.filter(pk=workspace.pk).update(organization=organization)

    # Rows written before the expand migration carry an empty permission set.
    # Empty is indistinguishable from a working deny, so derive rather than
    # leave it: the failure mode of not doing this is a locked-out user.
    for membership in Membership.objects.filter(permissions=[]).iterator(chunk_size=BATCH):
        Membership.objects.filter(pk=membership.pk).update(
            permissions=PRESETS.get(membership.role, PRESETS["VIEWER"])
        )


def unbackfill(apps, schema_editor):
    """Reverse is a no-op, deliberately.

    Re-adding the dropped columns is what `migrate 0009` does; refilling them
    from the organization would be guessing which workspace owned which value
    in a group with several. The reverse restores the schema, not the split.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0009_multi_currency_pricing"),
        ("billing", "0016_subscription_overage_item"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
