"""Tests for :class:`~fantasy_coach.clients.yahoo.YahooClient` (M2).

Every request is served by an ``httpx.MockTransport`` injected into M1's
``AuthedClient``, so the suite is fully offline — no live Yahoo call, and no
authenticated session required. The throttle and cache clocks are faked, so
timing behaviour is asserted exactly without sleeping.
"""

from __future__ import annotations

import httpx
import pytest

from fantasy_coach.clients.throttle import DRAFT_POLL_INTERVAL, NullThrottle, Throttle
from fantasy_coach.clients.yahoo import (
    PLAYER_PAGE_SIZE,
    YahooAPIError,
    YahooClient,
)
from tests.conftest import FakeClock, load_fixture


class FakeYahoo:
    """A routing MockTransport handler that records every request it serves.

    Routes are ``(url_fragment, response)`` pairs tried in order. A response is
    either a fixture name (served as 200 JSON) or an ``httpx.Response``.
    """

    def __init__(self, *routes: tuple[str, object]) -> None:
        self.routes = list(routes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for fragment, response in self.routes:
            if fragment in url:
                if isinstance(response, httpx.Response):
                    return response
                if callable(response):
                    return response(request)
                return httpx.Response(200, json=load_fixture(str(response)))
        return httpx.Response(404, json={"error": f"unrouted: {url}"})

    @property
    def urls(self) -> list[str]:
        """Every requested URL, in order."""
        return [str(request.url) for request in self.requests]

    @property
    def count(self) -> int:
        """How many HTTP requests were actually issued."""
        return len(self.requests)


def build_client(authed_factory, handler, **kwargs) -> YahooClient:
    """A YahooClient with throttling and caching off unless a test wants them."""
    kwargs.setdefault("throttle", NullThrottle())
    kwargs.setdefault("cache_ttl", 0)
    return YahooClient(authed_factory(handler), **kwargs)


LEAGUE = "449.l.123456"
TEAM = "449.l.123456.t.1"


# -- request construction ----------------------------------------------------


def test_every_request_asks_for_json(authed_factory):
    """Yahoo defaults to XML; ``format=json`` must be on every call (§2.3)."""
    fake = FakeYahoo(("game/nfl", "game_nfl"))
    build_client(authed_factory, fake).get_game()
    assert "format=json" in fake.urls[0]


def test_requests_go_to_the_yahoo_v2_base(authed_factory):
    fake = FakeYahoo(("game/nfl", "game_nfl"))
    build_client(authed_factory, fake).get_game()
    assert fake.urls[0].startswith(
        "https://fantasysports.yahooapis.com/fantasy/v2/game/nfl"
    )


def test_bearer_token_is_injected_by_the_m1_layer(authed_factory):
    """M2 never touches tokens — it inherits auth from ``AuthedClient``."""
    fake = FakeYahoo(("game/nfl", "game_nfl"))
    build_client(authed_factory, fake).get_game()
    assert fake.requests[0].headers["Authorization"] == "Bearer access-abc"


def test_raw_escape_hatch_returns_unmodelled_json(authed_factory):
    """Framework §2.4: keep a client that can hit any endpoint directly."""
    fake = FakeYahoo(("league/449.l.123456/settings", "league_settings"))
    client = build_client(authed_factory, fake)
    payload = client.raw(f"league/{LEAGUE}/settings")
    assert "fantasy_content" in payload


def test_raw_merges_extra_query_params(authed_factory):
    fake = FakeYahoo(("game/nfl", "game_nfl"))
    client = build_client(authed_factory, fake)
    client.raw("game/nfl", params={"foo": "bar"})
    assert "foo=bar" in fake.urls[0]
    assert "format=json" in fake.urls[0]


# -- discovery ---------------------------------------------------------------


def test_get_game_key_resolves_the_current_season(authed_factory):
    """§2.3 flags the numeric key as [verify at setup] — we read it live."""
    fake = FakeYahoo(("game/nfl", "game_nfl"))
    assert build_client(authed_factory, fake).get_game_key() == "449"


def test_get_user_leagues_returns_every_league(authed_factory):
    fake = FakeYahoo(("users;use_login=1", "user_leagues"))
    leagues = build_client(authed_factory, fake).get_user_leagues()
    assert [lg.league_key for lg in leagues] == [
        "449.l.123456", "449.l.987654", "423.l.111222",
    ]


def test_get_user_leagues_uses_the_nfl_alias_by_default(authed_factory):
    """No season → let Yahoo resolve the current in-season game server-side."""
    fake = FakeYahoo(("users;use_login=1", "user_leagues"))
    build_client(authed_factory, fake).get_user_leagues()
    assert "games;game_keys=nfl" in fake.urls[0]


def test_get_user_leagues_asks_by_season_when_given_one(authed_factory):
    """A season → ``game_codes``/``seasons``, so no numeric key is hard-coded."""
    fake = FakeYahoo(("users;use_login=1", "user_leagues"))
    leagues = build_client(authed_factory, fake).get_user_leagues(2026)
    assert "games;game_codes=nfl;seasons=2026" in fake.urls[0]
    assert [lg.league_key for lg in leagues] == ["449.l.123456", "449.l.987654"]


def test_get_user_leagues_filters_a_season_yahoo_failed_to_filter(authed_factory):
    """The fixture returns 2025 *and* 2026; asking for 2025 must yield one."""
    fake = FakeYahoo(("users;use_login=1", "user_leagues"))
    leagues = build_client(authed_factory, fake).get_user_leagues(2025)
    assert [lg.league_key for lg in leagues] == ["423.l.111222"]
    assert leagues[0].game_key == "423"


def test_explicit_game_key_overrides_season_handling(authed_factory):
    fake = FakeYahoo(("users;use_login=1", "user_leagues"))
    build_client(authed_factory, fake).get_user_leagues(game_key="423")
    assert "games;game_keys=423" in fake.urls[0]


# -- settings / teams / roster ----------------------------------------------


def test_get_league_settings(authed_factory):
    fake = FakeYahoo(("/settings", "league_settings"))
    settings = build_client(authed_factory, fake).get_league_settings(LEAGUE)
    assert settings.league_key == LEAGUE
    assert settings.ppr_type == "half_ppr"
    assert settings.starting_slots()["W/R/T"] == 1
    assert fake.urls[0].endswith("league/449.l.123456/settings?format=json")


def test_get_league_teams(authed_factory):
    fake = FakeYahoo(("/teams", "league_teams"))
    teams = build_client(authed_factory, fake).get_league_teams(LEAGUE)
    assert len(teams) == 3
    assert teams[0].is_owned_by_current_login is True


def test_get_team_roster(authed_factory):
    fake = FakeYahoo(("/roster", "team_roster"))
    roster = build_client(authed_factory, fake).get_team_roster(TEAM)
    assert roster.team_key == TEAM
    assert len(roster.starters) == 8
    assert len(roster.bench) == 2


def test_get_team_roster_selects_a_week(authed_factory):
    fake = FakeYahoo(("/roster", "team_roster"))
    build_client(authed_factory, fake).get_team_roster(TEAM, week=5)
    assert "roster;week=5" in fake.urls[0]


# -- player pagination -------------------------------------------------------


def players_routes() -> FakeYahoo:
    """Route the paginated player pool: a full page, then a short one."""
    return FakeYahoo(
        ("start=0;", "league_players_page1"),
        ("start=25;", "league_players_page2"),
        ("start=50;", "league_players_empty"),
    )


def test_get_players_paginates_until_a_short_page(authed_factory):
    """Yahoo caps at 25/page; a short page is the end-of-data signal (§2.3)."""
    fake = players_routes()
    players = build_client(authed_factory, fake).get_players(LEAGUE)
    assert len(players) == 32  # 25 + 7
    assert fake.count == 2
    assert "start=0;count=25" in fake.urls[0]
    assert "start=25;count=25" in fake.urls[1]


def test_get_players_stops_on_an_empty_page(authed_factory):
    """When the pool is an exact multiple of 25 the last page is empty."""
    fake = FakeYahoo(
        ("start=0;", "league_players_page1"),
        ("start=25;", "league_players_empty"),
    )
    players = build_client(authed_factory, fake).get_players(LEAGUE)
    assert len(players) == 25
    assert fake.count == 2


def test_get_players_respects_max_players(authed_factory):
    """A cap must stop the loop early — it bounds total requests, not just output."""
    fake = players_routes()
    players = build_client(authed_factory, fake).get_players(LEAGUE, max_players=10)
    assert len(players) == 10
    assert fake.count == 1


def test_get_players_max_across_a_page_boundary(authed_factory):
    fake = players_routes()
    players = build_client(authed_factory, fake).get_players(LEAGUE, max_players=30)
    assert len(players) == 30
    assert fake.count == 2


def test_get_players_pagination_preserves_order(authed_factory):
    fake = players_routes()
    players = build_client(authed_factory, fake).get_players(LEAGUE)
    assert players[0].full_name == "Ja'Marr Chase"
    assert players[24].full_name == "Tetairoa McMillan"  # last of page 1
    assert players[25].full_name == "Tony Pollard"  # first of page 2
    assert len({p.player_key for p in players}) == 32


def test_get_players_builds_matrix_selectors(authed_factory):
    """Filters are Yahoo matrix selectors, not query params (§2.3)."""
    fake = players_routes()
    build_client(authed_factory, fake).get_players(
        LEAGUE, status="A", position="RB", sort="AR", max_players=1
    )
    url = fake.urls[0]
    assert "players;status=A;position=RB;sort=AR;start=0;count=25" in url


def test_get_players_requests_out_subresources(authed_factory):
    """One request per page carrying ownership + ADP + ranks, not one per player."""
    fake = players_routes()
    build_client(authed_factory, fake).get_players(
        LEAGUE, out=("percent_owned", "draft_analysis", "ranks"), max_players=1
    )
    assert "out=percent_owned,draft_analysis,ranks" in fake.urls[0]


def test_get_players_chains_a_single_subresource_with_a_slash(authed_factory):
    fake = FakeYahoo(("percent_owned", "league_players_page1"))
    build_client(authed_factory, fake).get_players(
        LEAGUE, sub_resource="percent_owned", max_players=1
    )
    assert fake.urls[0].split("?")[0].endswith("/percent_owned")


def test_get_players_by_explicit_keys_does_not_paginate(authed_factory):
    fake = FakeYahoo(("player_keys=", "league_players_page2"))
    players = build_client(authed_factory, fake).get_players(
        LEAGUE, player_keys=["449.p.31030", "449.p.32705"]
    )
    assert fake.count == 1
    assert "player_keys=449.p.31030,449.p.32705" in fake.urls[0]
    assert len(players) == 7


def test_page_size_constant_matches_yahoos_cap():
    assert PLAYER_PAGE_SIZE == 25


# -- draft / transactions / matchups ----------------------------------------


def test_get_draft_results(authed_factory):
    fake = FakeYahoo(("draftresults", "draft_results"))
    picks = build_client(authed_factory, fake).get_draft_results(LEAGUE)
    assert [p.pick for p in picks] == list(range(1, 16))
    assert sum(1 for p in picks if p.is_made) == 13


def test_get_transactions(authed_factory):
    fake = FakeYahoo(("transactions", "transactions"))
    transactions = build_client(authed_factory, fake).get_transactions(LEAGUE)
    assert [t.type for t in transactions] == ["add/drop", "trade", "add"]


def test_get_transactions_applies_filters(authed_factory):
    fake = FakeYahoo(("transactions", "transactions"))
    build_client(authed_factory, fake).get_transactions(
        LEAGUE, types=["add", "drop"], count=10
    )
    assert "transactions;types=add,drop;count=10" in fake.urls[0]


def test_get_matchups_takes_week_positionally(authed_factory):
    fake = FakeYahoo(("scoreboard", "scoreboard_week1"))
    matchups = build_client(authed_factory, fake).get_matchups(LEAGUE, 1)
    assert len(matchups) == 2
    assert "scoreboard;week=1" in fake.urls[0]


def test_get_matchups_without_a_week(authed_factory):
    fake = FakeYahoo(("scoreboard", "scoreboard_week1"))
    build_client(authed_factory, fake).get_matchups(LEAGUE)
    assert fake.urls[0].endswith("scoreboard?format=json")


# -- throttling --------------------------------------------------------------


def test_the_default_client_is_already_draft_poll_safe(authed_factory):
    """Framework §7: a naive poll loop must not be able to hammer Yahoo."""
    client = YahooClient(authed_factory(FakeYahoo()), cache_ttl=0)
    assert client.throttle.min_interval == DRAFT_POLL_INTERVAL


def test_every_request_passes_through_the_throttle(authed_factory):
    clock = FakeClock()
    throttle = Throttle(2.5, time_func=clock.time, sleep_func=clock.sleep)
    fake = players_routes()

    client = build_client(authed_factory, fake, throttle=throttle)
    client.get_players(LEAGUE)

    assert fake.count == 2
    # First request free, second spaced by the full interval.
    assert clock.sleeps == [2.5]


def test_draft_polling_stays_at_the_configured_rate(authed_factory):
    """Five polls at a 2.5s floor take 10s of spacing — ~0.4 req/s."""
    clock = FakeClock()
    throttle = Throttle(DRAFT_POLL_INTERVAL, time_func=clock.time,
                        sleep_func=clock.sleep)
    fake = FakeYahoo(("draftresults", "draft_results"))
    client = build_client(authed_factory, fake, throttle=throttle)

    start = clock.time()
    for _ in range(5):
        client.get_draft_results(LEAGUE)

    assert fake.count == 5
    assert clock.time() - start == 10.0


# -- caching -----------------------------------------------------------------


def test_warm_reads_are_cached(authed_factory):
    """§7: warm the static data pre-draft so the draft itself is nearly free."""
    fake = FakeYahoo(("/settings", "league_settings"))
    clock = FakeClock()
    client = build_client(
        authed_factory, fake, cache_ttl=900, time_func=clock.time
    )

    first = client.get_league_settings(LEAGUE)
    second = client.get_league_settings(LEAGUE)

    assert fake.count == 1
    assert first.league_key == second.league_key
    assert client.cache.stats["hits"] == 1


def test_cache_entries_expire(authed_factory):
    fake = FakeYahoo(("/settings", "league_settings"))
    clock = FakeClock()
    client = build_client(
        authed_factory, fake, cache_ttl=900, time_func=clock.time
    )

    client.get_league_settings(LEAGUE)
    clock.advance(901)
    client.get_league_settings(LEAGUE)

    assert fake.count == 2


def test_different_paths_cache_separately(authed_factory):
    fake = FakeYahoo(("/settings", "league_settings"), ("/teams", "league_teams"))
    clock = FakeClock()
    client = build_client(
        authed_factory, fake, cache_ttl=900, time_func=clock.time
    )

    client.get_league_settings(LEAGUE)
    client.get_league_teams(LEAGUE)
    client.get_league_settings(LEAGUE)

    assert fake.count == 2


def test_draft_results_are_never_cached(authed_factory):
    """A cached draft board mid-round is worse than useless."""
    fake = FakeYahoo(("draftresults", "draft_results"))
    clock = FakeClock()
    client = build_client(
        authed_factory, fake, cache_ttl=900, time_func=clock.time
    )

    for _ in range(3):
        client.get_draft_results(LEAGUE)

    assert fake.count == 3


def test_clear_cache_forces_a_refetch(authed_factory):
    fake = FakeYahoo(("/settings", "league_settings"))
    client = build_client(authed_factory, fake, cache_ttl=900)

    client.get_league_settings(LEAGUE)
    client.clear_cache()
    client.get_league_settings(LEAGUE)

    assert fake.count == 2


def test_cache_ttl_zero_disables_caching(authed_factory):
    fake = FakeYahoo(("/settings", "league_settings"))
    client = build_client(authed_factory, fake, cache_ttl=0)

    client.get_league_settings(LEAGUE)
    client.get_league_settings(LEAGUE)

    assert fake.count == 2


# -- errors, retries, backoff ------------------------------------------------


def test_throttle_response_is_retried_with_backoff(authed_factory):
    """Yahoo's 999 sentinel → exponential backoff, then success (§7)."""
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(999, text="throttled")
        return httpx.Response(200, json=load_fixture("game_nfl"))

    clock = FakeClock()
    throttle = Throttle(0.0, time_func=clock.time, sleep_func=clock.sleep)
    fake = FakeYahoo(("game/nfl", flaky))

    client = build_client(authed_factory, fake, throttle=throttle)
    assert client.get_game().game_key == "449"
    assert attempts["n"] == 3
    assert clock.sleeps == [2.0, 4.0]


def test_http_429_is_also_retried(authed_factory):
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=load_fixture("game_nfl"))

    fake = FakeYahoo(("game/nfl", flaky))
    client = build_client(authed_factory, fake)
    assert client.get_game().game_key == "449"
    assert attempts["n"] == 2


def test_exhausted_retries_raise(authed_factory):
    fake = FakeYahoo(("game/nfl", httpx.Response(999, text="throttled")))
    client = build_client(authed_factory, fake, max_retries=3)

    with pytest.raises(YahooAPIError) as excinfo:
        client.get_game()

    assert excinfo.value.status_code == 999
    assert fake.count == 3


def test_client_errors_are_not_retried(authed_factory):
    """A 404 is a bad key, not a throttle — retrying only wastes budget."""
    fake = FakeYahoo(("league/", httpx.Response(404, text="not found")))
    client = build_client(authed_factory, fake)

    with pytest.raises(YahooAPIError) as excinfo:
        client.get_league_settings("449.l.000000")

    assert excinfo.value.status_code == 404
    assert fake.count == 1


def test_non_json_body_raises_a_useful_error(authed_factory):
    """Yahoo sometimes answers with XML or an HTML error page."""
    fake = FakeYahoo(("game/nfl", httpx.Response(200, text="<xml>nope</xml>")))
    client = build_client(authed_factory, fake)

    with pytest.raises(YahooAPIError, match="non-JSON body"):
        client.get_game()


def test_error_message_names_the_url(authed_factory):
    fake = FakeYahoo(("league/", httpx.Response(500, text="boom")))
    client = build_client(authed_factory, fake)

    with pytest.raises(YahooAPIError, match="league/449.l.123456/teams"):
        client.get_league_teams(LEAGUE)


# -- lifecycle ---------------------------------------------------------------


def test_client_works_as_a_context_manager(authed_factory):
    fake = FakeYahoo(("game/nfl", "game_nfl"))
    with build_client(authed_factory, fake) as client:
        assert client.get_game().season == "2026"
