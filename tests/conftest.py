"""Shared pytest fixtures and helpers.

Everything here is offline: HTTP is faked with ``httpx.MockTransport`` so no
test ever touches the network (framework requirement for M1, carried into M2).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from fantasy_coach.auth.oauth import YahooOAuthClient
from fantasy_coach.auth.session import AuthedClient
from fantasy_coach.auth.token_store import Token, TokenStore

#: Recorded-shape Yahoo Fantasy JSON responses (see ``tests/fixtures``).
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    """A throwaway path for a token file inside pytest's tmp dir."""
    return tmp_path / ".tokens.json"


@pytest.fixture
def token_store(token_path: Path) -> TokenStore:
    """A TokenStore pointed at a throwaway path."""
    return TokenStore(token_path)


@pytest.fixture
def fresh_token() -> Token:
    """A token that is valid for another hour."""
    return Token(
        access_token="access-abc",
        refresh_token="refresh-xyz",
        expires_at=time.time() + 3600,
        token_type="bearer",
        xoauth_yahoo_guid="GUID123",
        scope="fspt-r",
    )


def make_mock_client(handler) -> httpx.Client:
    """Build an ``httpx.Client`` whose requests are served by ``handler``.

    Args:
        handler: A callable ``(httpx.Request) -> httpx.Response``.
    """
    return httpx.Client(transport=httpx.MockTransport(handler))


# -- M2 helpers --------------------------------------------------------------


def load_fixture(name: str) -> dict:
    """Load a recorded Yahoo JSON fixture by filename (with or without ``.json``)."""
    if not name.endswith(".json"):
        name += ".json"
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClock:
    """A manual monotonic clock whose ``sleep`` advances time instead of waiting.

    Injected into :class:`~fantasy_coach.clients.throttle.Throttle` and the
    Yahoo client's cache so timing behaviour is asserted exactly, with zero
    real wall-clock delay.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        """Current fake time in seconds."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Record the sleep and advance the clock by that much."""
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        """Move time forward without recording a sleep."""
        self.now += seconds

    @property
    def total_slept(self) -> float:
        """Sum of every recorded sleep."""
        return sum(self.sleeps)


# -- M3 helpers --------------------------------------------------------------


def make_identity(
    *,
    yahoo_player_id: str,
    full_name: str = "",
    team_abbr: str = "",
    position: str = "",
    position_type: str = "",
    bye_week=None,
    status: str = "",
    first_name: str = "",
    last_name: str = "",
):
    """Build a :class:`PlayerIdentity` for resolver/index tests.

    Kept out of the source models' import chain so a test only needs this one
    helper. Mirrors what ``Player.identity()`` produces in M2.
    """
    from fantasy_coach.clients.models import PlayerIdentity

    return PlayerIdentity(
        yahoo_player_id=yahoo_player_id,
        yahoo_player_key=f"449.p.{yahoo_player_id}" if yahoo_player_id else "",
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        team_abbr=team_abbr,
        position=position,
        position_type=position_type,
        bye_week=bye_week,
        status=status,
    )


@pytest.fixture
def id_map_rows() -> list[dict]:
    """The recorded id-map sample (import_ids()-shaped records)."""
    return load_fixture("id_map_sample")


@pytest.fixture
def crosswalk(id_map_rows):
    """An :class:`IdCrosswalk` built offline from the id-map fixture."""
    from fantasy_coach.ingest.crosswalk import load_id_crosswalk

    return load_id_crosswalk(rows=id_map_rows)


@pytest.fixture
def authed_factory(token_store: TokenStore, fresh_token: Token):
    """Return ``make(handler) -> AuthedClient`` backed by a stored fresh token.

    The API transport is the caller's ``handler``; the OAuth token endpoint is
    stubbed to fail loudly, because a fresh token means no test in M2 should
    ever trigger a refresh.
    """
    token_store.save(fresh_token)

    def refresh_must_not_happen(request: httpx.Request) -> httpx.Response:
        raise AssertionError("M2 tests must not trigger a token refresh")

    def make(handler) -> AuthedClient:
        oauth = YahooOAuthClient(
            client_id="test-id",
            client_secret="test-secret",
            redirect_uri="https://localhost:8000/callback",
            http=make_mock_client(refresh_must_not_happen),
        )
        return AuthedClient(oauth, token_store, http=make_mock_client(handler))

    return make
