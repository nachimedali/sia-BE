"""Provider-backed option lists — Pinterest boards (P4-02).

**The one genuinely new provider capability in Phase 4.** A board id typed by
hand is a rejected pin, so the composer has to offer the account's real
boards; there is no way to know them without asking the provider.

Kept deliberately narrow: this is a *read* of reference data, like listing the
Pages a Facebook account can post as. It is not part of the adaptation engine
and touches nothing `render_post` does — the option itself is one row in
`rules.py`, and `source` is what points the composer here.
"""

from __future__ import annotations

from typing import Any

import pytest

from content.models import Platform

pytestmark = pytest.mark.django_db


@pytest.fixture
def pinterest_account(paid_workspace: Any) -> Any:
    from channels.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=paid_workspace,
        platform=Platform.PINTEREST,
        provider_account_id="pin-acct-1",
        display_name="Acme Pinterest",
    )


class TestTheFakeAnswers:
    def test_the_fake_returns_boards_so_a_fresh_checkout_works(
        self, pinterest_account: Any, platform_adapter: Any
    ) -> None:
        """Part 7 rule 6 — every external dependency is a port with a fake, and
        a fresh checkout runs end to end with no third-party account."""
        boards = platform_adapter.list_remote_options(
            platform=Platform.PINTEREST,
            provider_account_id=pinterest_account.provider_account_id,
            source="pinterest_boards",
        )

        assert boards
        assert {"id", "name"} <= set(boards[0])

    def test_an_unknown_source_is_empty_rather_than_an_error(
        self, pinterest_account: Any, platform_adapter: Any
    ) -> None:
        # A source nothing declares is a composer bug, not a provider outage —
        # answering with nothing is what lets the form render an empty select
        # instead of a 500.
        assert (
            platform_adapter.list_remote_options(
                platform=Platform.PINTEREST,
                provider_account_id="pin-acct-1",
                source="nope",
            )
            == []
        )


class TestTheEndpoint:
    URL = "/api/v1/channels/{id}/options/{source}/"

    def test_it_lists_the_boards(
        self, auth_client: Any, pinterest_account: Any, platform_adapter: Any
    ) -> None:
        response = auth_client.get(
            self.URL.format(id=pinterest_account.id, source="pinterest_boards")
        )

        assert response.status_code == 200, response.json()
        assert response.json()["options"]

    def test_an_undeclared_source_is_a_400(self, auth_client: Any, pinterest_account: Any) -> None:
        """Checked against `rules.py`, not passed through. Forwarding an
        arbitrary string to the provider would make this endpoint a proxy for
        any call the vendor exposes."""
        response = auth_client.get(self.URL.format(id=pinterest_account.id, source="anything"))

        assert response.status_code == 400

    def test_a_source_the_platform_does_not_declare_is_a_400(
        self, auth_client: Any, paid_workspace: Any
    ) -> None:
        # `pinterest_boards` is real, but not for an Instagram account.
        from channels.models import SocialAccount

        instagram = SocialAccount.objects.create(
            workspace=paid_workspace,
            platform=Platform.INSTAGRAM,
            provider_account_id="ig-1",
        )

        response = auth_client.get(self.URL.format(id=instagram.id, source="pinterest_boards"))

        assert response.status_code == 400

    def test_another_workspaces_account_is_404(
        self, auth_client: Any, other_user: Any, paid_workspace: Any
    ) -> None:
        # `paid_workspace`, not `workspace`: this ViewSet sits behind
        # `HasFeature("auto_publish")`, and a 402 from that gate would mask
        # whether the tenancy filter underneath it works at all — the same
        # reason the router sweep provisions a paid plan.
        from channels.models import SocialAccount
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        account = SocialAccount.objects.create(
            workspace=theirs, platform=Platform.PINTEREST, provider_account_id="theirs"
        )

        response = auth_client.get(self.URL.format(id=account.id, source="pinterest_boards"))

        assert response.status_code == 404


class TestTheDeclarationPointsHere:
    def test_every_remote_choice_option_names_a_source(self) -> None:
        from content.services.rules import PLATFORM_RULES

        for platform, rule in PLATFORM_RULES.items():
            for option in rule.options:
                if option.kind == "remote_choice":
                    assert option.source, f"{platform}.{option.key} has nowhere to fetch from"

    def test_every_declared_source_is_one_the_adapter_knows(self) -> None:
        """A `source` the adapter cannot serve would render an empty select
        that never fills — the composer would look broken with nothing in the
        logs to say why."""
        from channels.adapters.base import REMOTE_OPTION_SOURCES
        from content.services.rules import PLATFORM_RULES

        for rule in PLATFORM_RULES.values():
            for option in rule.options:
                if option.kind == "remote_choice":
                    assert option.source in REMOTE_OPTION_SOURCES
