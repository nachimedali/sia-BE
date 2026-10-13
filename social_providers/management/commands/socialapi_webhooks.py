"""Register and look after the SocialAPI webhook endpoint (SA-41).

    manage.py socialapi_webhooks register --url https://<host>/api/v1/webhooks/socialapi/
    manage.py socialapi_webhooks status
    manage.py socialapi_webhooks test <endpoint id>
    manage.py socialapi_webhooks rotate --url https://<host>/api/v1/webhooks/socialapi/
    manage.py socialapi_webhooks retire <endpoint id>

**Nothing secret is written anywhere but the terminal.** The vendor shows an
endpoint's secret once, when it is created; this prints it once, for the
operator to put in `SOCIALAPI_WEBHOOK_SECRET` (the git-ignored `.env`, or the
deployment's secrets). The database never holds it.

Registration needs the endpoint reachable over HTTPS first: the vendor pings it
and refuses to save an endpoint that does not answer. In development that means
a tunnel (`cloudflared tunnel --url http://localhost:8000`, or ngrok) — see
`CLAUDE.md`, *SocialAPI — operator steps owed*.

**Rotation without losing a delivery**: `rotate` registers a second endpoint
with a new secret and stops. The operator moves the current secret to
`SOCIALAPI_WEBHOOK_SECRET_PREVIOUS`, puts the new one in
`SOCIALAPI_WEBHOOK_SECRET`, restarts, and only then retires the old endpoint —
both secrets verify in between.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Count, Max
from django.utils import timezone

from social_providers.models import WebhookDelivery
from social_providers.socialapi.client import SocialAPIClient
from social_providers.socialapi.connect import rows
from social_providers.socialapi.errors import SocialAPIError
from social_providers.socialapi.events import SUBSCRIBED
from social_providers.socialapi.webhooks import PROVIDER, counts


class Command(BaseCommand):
    help = "Register, inspect, test, rotate or retire the SocialAPI webhook endpoint."

    def add_arguments(self, parser: CommandParser) -> None:
        actions = parser.add_subparsers(dest="action", required=True)
        register = actions.add_parser("register", help="Create the endpoint; prints its secret.")
        register.add_argument("--url", required=True)
        actions.add_parser("status", help="The endpoints at the vendor and what arrived here.")
        test = actions.add_parser("test", help="Ask the vendor to send a test delivery.")
        test.add_argument("endpoint_id")
        rotate = actions.add_parser("rotate", help="Create a second endpoint with a new secret.")
        rotate.add_argument("--url", required=True)
        retire = actions.add_parser("retire", help="Delete an endpoint at the vendor.")
        retire.add_argument("endpoint_id")
        retire.add_argument(
            "--force", action="store_true", help="Even if it is the only active endpoint."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        self.client = SocialAPIClient()
        action = options["action"]
        try:
            if action == "register":
                self._register(options["url"], rotating=False)
            elif action == "rotate":
                self._register(options["url"], rotating=True)
            elif action == "status":
                self._status()
            elif action == "test":
                self.client.post(f"/webhooks/{options['endpoint_id']}/test")
                self.stdout.write(self.style.SUCCESS("Test delivery requested."))
            elif action == "retire":
                self._retire(options["endpoint_id"], force=options["force"])
        except SocialAPIError as error:
            raise CommandError(
                f"SocialAPI refused: {error.message} "
                f"(kind={error.kind}, status={error.status}, request_id={error.request_id or '-'})"
            ) from error

    # --- actions -----------------------------------------------------------------------
    def _register(self, url: str, *, rotating: bool) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise CommandError("The endpoint must be an https:// URL the vendor can reach.")
        if parsed.hostname in ("localhost", "127.0.0.1") or parsed.hostname.endswith(".local"):
            raise CommandError(
                "The vendor cannot reach this machine directly. Start a tunnel "
                "(cloudflared tunnel --url http://localhost:8000) and register its https URL."
            )
        existing = [row for row in self._endpoints() if row.get("url") == url]
        if existing and not rotating:
            raise CommandError(
                f"An endpoint for this URL already exists ({existing[0].get('id')}). "
                "Use `rotate` for a new secret, or `retire` it first."
            )
        answer = self.client.post("/webhooks", json={"url": url, "events": list(SUBSCRIBED)})
        created = answer.data or {}
        secret = str(created.get("secret") or "")
        self.stdout.write(self.style.SUCCESS(f"Endpoint {created.get('id')} registered for {url}."))
        self.stdout.write(f"Events: {', '.join(SUBSCRIBED)}")
        if not secret:
            self.stdout.write(
                self.style.WARNING("The vendor returned no secret; check the dashboard.")
            )
            return
        self.stdout.write("")
        self.stdout.write("Secret (shown once, never stored here):")
        self.stdout.write(f"  {secret}")
        self.stdout.write("")
        if rotating:
            self.stdout.write(
                "Next: move the current SOCIALAPI_WEBHOOK_SECRET to "
                "SOCIALAPI_WEBHOOK_SECRET_PREVIOUS, set SOCIALAPI_WEBHOOK_SECRET to the secret "
                "above, restart, then `socialapi_webhooks retire <old endpoint id>`."
            )
        else:
            self.stdout.write(
                "Next: set SOCIALAPI_WEBHOOK_SECRET to the secret above (app-BE/.env, or the "
                "deployment's secrets) and restart the web and worker processes."
            )

    def _retire(self, endpoint_id: str, *, force: bool) -> None:
        active = [row for row in self._endpoints() if row.get("is_active", True)]
        if not force and [str(row.get("id")) for row in active] == [endpoint_id]:
            raise CommandError(
                "That is the only active endpoint: retiring it stops every delivery. "
                "Register its replacement first, or pass --force."
            )
        self.client.delete(f"/webhooks/{endpoint_id}")
        self.stdout.write(self.style.SUCCESS(f"Endpoint {endpoint_id} retired."))
        if getattr(settings, "SOCIALAPI_WEBHOOK_SECRET_PREVIOUS", ""):
            self.stdout.write("You can now clear SOCIALAPI_WEBHOOK_SECRET_PREVIOUS.")

    def _status(self) -> None:
        secret = "set" if getattr(settings, "SOCIALAPI_WEBHOOK_SECRET", "") else "NOT SET"
        previous = "set" if getattr(settings, "SOCIALAPI_WEBHOOK_SECRET_PREVIOUS", "") else "-"
        self.stdout.write(f"Secret: {secret}   previous (rotation): {previous}")
        endpoints = self._endpoints()
        if not endpoints:
            self.stdout.write(
                self.style.WARNING("No endpoint registered: outcomes arrive by polling only.")
            )
        for row in endpoints:
            state = "active" if row.get("is_active", True) else "paused"
            self.stdout.write(f"  {row.get('id')}  {state}  {row.get('url')}")
            missing = sorted(set(SUBSCRIBED) - set(row.get("events") or []))
            if missing:
                self.stdout.write(f"    not subscribed: {', '.join(missing)}")

        since = timezone.now() - dt.timedelta(hours=24)
        recent = WebhookDelivery.objects.filter(provider=PROVIDER, received_at__gte=since)
        last = WebhookDelivery.objects.filter(provider=PROVIDER).aggregate(at=Max("received_at"))
        self.stdout.write(f"Last delivery received: {last['at'] or 'never'}")
        by_status = dict(recent.values_list("status").annotate(n=Count("id")))
        self.stdout.write(f"Last 24 h by status: {by_status or 'none'}")
        today = counts(timezone.now().date())
        self.stdout.write(f"Refused today: {today}")

    def _endpoints(self) -> list[dict[str, Any]]:
        return rows(self.client.get("/webhooks").data)
