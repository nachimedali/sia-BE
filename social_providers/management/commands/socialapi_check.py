"""Check the social provider from the command line — read-only, spends nothing.

    manage.py socialapi_check

Prints which provider is selected and which ports are served by it or by the
fakes, then calls SocialAPI through the real client (budget, error taxonomy,
call log and all): `GET /usage` and `GET /accounts`. Both are free reads. Exit
status 1 on any failure, so a deploy script can gate on it.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from social_providers import registry
from social_providers.socialapi.client import SocialAPIClient
from social_providers.socialapi.errors import SocialAPIError


class Command(BaseCommand):
    help = "Read-only check of the selected social provider and the SocialAPI connection."

    def handle(self, *args: Any, **options: Any) -> None:
        state = registry.status()
        self.stdout.write(f"Provider: {state['label']} ({state['provider']})")
        self.stdout.write(f"API key: {'present' if state['has_credential'] else 'MISSING'}")
        self.stdout.write(f"Fake mode: {state['fake_mode']}")
        for port, info in state["ports"].items():
            built = "built" if info["built"] else "not built yet"
            self.stdout.write(f"  {port:<9} {built:<14} served by: {info['serving']}")

        if state["provider"] != "socialapi":
            self.stdout.write("SocialAPI is not the selected provider; nothing to call.")
            return

        client = SocialAPIClient()
        try:
            usage = client.get("/usage")
            accounts = client.get("/accounts")
        except SocialAPIError as error:
            raise CommandError(
                f"SocialAPI check failed: {error.message} "
                f"(kind={error.kind}, status={error.status}, request_id={error.request_id or '-'})"
            ) from error

        u = usage.data or {}

        def cap(used: Any, limit: Any) -> str:
            return f"{used}/{'unlimited' if limit == -1 else limit}"

        self.stdout.write(self.style.SUCCESS("SocialAPI reachable."))
        self.stdout.write(f"  Period: {u.get('period_start', '?')} to {u.get('period_end', '?')}")
        self.stdout.write(f"  Brands: {cap(u.get('brands_used'), u.get('brands_limit'))}")
        self.stdout.write(f"  Posts: {cap(u.get('posts_used'), u.get('posts_limit'))}")
        self.stdout.write(
            f"  Interactions: {cap(u.get('interactions_used'), u.get('interactions_limit'))}"
        )
        self.stdout.write(f"  Facebook Pages per brand: {u.get('page_allowance', '?')}")
        rate = usage.rate
        self.stdout.write(f"  Rate limit: {rate.limit}/min, {rate.remaining} left this minute")
        rows = (
            accounts.data
            if isinstance(accounts.data, list)
            else (accounts.data or {}).get("data", [])
        )
        rows = rows if isinstance(rows, list) else []
        platforms = sorted({str(row.get("platform", "?")) for row in rows if isinstance(row, dict)})
        self.stdout.write(
            f"  Connected accounts: {len(rows)}"
            + (f" ({', '.join(platforms)})" if platforms else "")
        )
        self.stdout.write(
            f"  Request ids: usage={usage.request_id or '-'} accounts={accounts.request_id or '-'}"
        )
