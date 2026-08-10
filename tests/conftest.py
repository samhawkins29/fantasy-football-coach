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


# --------------------------------------------------------------------------- #
# M5 draft-companion fixtures — a small, hand-computable league + warmed store
# --------------------------------------------------------------------------- #

#: League key every draft test shares (2-team league keeps demand tiny).
DRAFT_LEAGUE_KEY = "sim.l.draft"

#: (canonical_id, name, position, projected points, yahoo_id, adp).
#: With num_teams=2 and the roster below, replacement baselines work out to
#: QB 180 / RB 200 / WR 205 / TE 110, so VORPs are hand-checkable:
#: R1..R6 = 100/80/60/40/20/0, W1..W6 = 45/35/25/15/5/0, Q1..Q4 = 20/10/0/-10,
#: T1..T3 = 70/10/0. W6 deliberately has no yahoo id (canonical-id fallback).
DRAFT_POOL = [
    ("Q1", "Quincy One", "QB", 200.0, "101", 8.0),
    ("Q2", "Quincy Two", "QB", 190.0, "102", 20.0),
    ("Q3", "Quincy Three", "QB", 180.0, "103", None),
    ("Q4", "Quincy Four", "QB", 170.0, "104", None),
    ("R1", "Rusher One", "RB", 300.0, "201", 1.0),
    ("R2", "Rusher Two", "RB", 280.0, "202", 2.0),
    ("R3", "Rusher Three", "RB", 260.0, "203", 4.0),
    ("R4", "Rusher Four", "RB", 240.0, "204", 6.0),
    ("R5", "Rusher Five", "RB", 220.0, "205", 10.0),
    ("R6", "Rusher Six", "RB", 200.0, "206", None),
    ("W1", "Wideout One", "WR", 250.0, "301", 3.0),
    ("W2", "Wideout Two", "WR", 240.0, "302", 5.0),
    ("W3", "Wideout Three", "WR", 230.0, "303", 7.0),
    ("W4", "Wideout Four", "WR", 220.0, "304", 9.0),
    ("W5", "Wideout Five", "WR", 210.0, "305", None),
    ("W6", "Wideout Six", "WR", 205.0, None, None),
    ("T1", "Tightend One", "TE", 180.0, "401", 12.0),
    ("T2", "Tightend Two", "TE", 120.0, "402", None),
    ("T3", "Tightend Three", "TE", 110.0, "403", None),
]


@pytest.fixture
def draft_settings():
    """2-team league: QB/2RB/2WR/TE/W-R-T flex/1 BN/1 IR → 8 draft rounds."""
    from fantasy_coach.clients.models import LeagueSettings, RosterPosition

    return LeagueSettings(
        league_key=DRAFT_LEAGUE_KEY,
        max_teams=2,
        roster_positions=[
            RosterPosition(position="QB", count=1),
            RosterPosition(position="RB", count=2),
            RosterPosition(position="WR", count=2),
            RosterPosition(position="TE", count=1),
            RosterPosition(position="W/R/T", count=1),
            RosterPosition(position="BN", count=1, is_starting_position=False),
            RosterPosition(position="IR", count=1, is_starting_position=False),
        ],
    )


def make_draft_pool():
    """The pool as (CanonicalPlayers, ProjectionRecords), rebuilt per test."""
    from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
    from fantasy_coach.ingest.sources import ProjectionRecord

    players, projections = [], []
    for cid, name, pos, points, yahoo_id, adp in DRAFT_POOL:
        player = CanonicalPlayer(
            canonical_id=cid,
            ids=ExternalIds(gsis_id=cid, yahoo_id=yahoo_id),
            name=name,
            position=pos,
            team="KC",
            resolution_method="test",
        )
        if adp is not None:
            player.market.adp = adp
        players.append(player)
        projections.append(
            ProjectionRecord(
                source="test",
                source_id=cid,
                source_id_field="gsis_id",
                points=points,
                position=pos,
                team="KC",
                name=name,
                stats={},  # empty -> the board scores from `points` directly
            )
        )
    return players, projections


@pytest.fixture
def draft_store(draft_settings):
    """An in-memory store warmed with the draft pool (players+ADP+board)."""
    from fantasy_coach.store import CoachStore, warm_store

    store = CoachStore(":memory:")
    players, projections = make_draft_pool()
    warm_store(
        store, draft_settings, projections=projections, players=players, season=2026
    )
    yield store
    store.close()


def make_pick(pick, team_key, raw_id, *, num_teams=2, game="sim"):
    """A made DraftPick for the draft-loop tests (round derived from pick)."""
    from fantasy_coach.clients.models import DraftPick

    return DraftPick(
        pick=pick,
        round=(pick - 1) // num_teams + 1,
        team_key=team_key,
        player_key=f"{game}.p.{raw_id}",
    )
