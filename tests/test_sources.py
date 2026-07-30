"""Tests for the external feed sources (M3, framework §2.5).

All offline: nflverse fetchers are injected; Sleeper runs on an
``httpx.MockTransport`` (the M2 pattern); the key-gated sources are exercised
via their pure derivations and their not-configured guards.
"""

from __future__ import annotations

import httpx
import pytest

from fantasy_coach.ingest.sources import (
    DataSource,
    FantasyProsSource,
    NflverseSource,
    OddsApiSource,
    OpenMeteoSource,
    ProjectionRecord,
    ProjectionSource,
    SleeperSource,
    SourceNotConfigured,
)

from .conftest import load_fixture, make_mock_client


# -- nflverse (injected fetchers, no network, no pandas) --------------------


def test_nflverse_uses_injected_fetchers():
    calls = {}

    def fake_weekly(years):
        calls["weekly"] = years
        return [{"player_id": "x", "week": 1}]

    src = NflverseSource(fetchers={"weekly": fake_weekly})
    out = src.weekly_stats([2024, 2025])
    assert calls["weekly"] == [2024, 2025]
    assert out == [{"player_id": "x", "week": 1}]
    assert src.is_live


def test_nflverse_id_map_passthrough():
    src = NflverseSource(fetchers={"ids": lambda: [{"gsis_id": "00-1"}]})
    assert src.id_map() == [{"gsis_id": "00-1"}]


def test_nflverse_satisfies_datasource_protocol():
    assert isinstance(NflverseSource(), DataSource)


# -- Sleeper (MockTransport) -------------------------------------------------


def _sleeper_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/players/nfl":
        return httpx.Response(200, json=load_fixture("sleeper_players_sample"))
    if path.startswith("/v1/players/nfl/trending/add"):
        return httpx.Response(200, json=load_fixture("sleeper_trending_add"))
    return httpx.Response(404, json={})


def test_sleeper_players_blob():
    src = SleeperSource(client=make_mock_client(_sleeper_handler))
    players = src.players()
    assert "11560" in players
    assert players["11560"]["yahoo_id"] == 40155


def test_sleeper_trending():
    src = SleeperSource(client=make_mock_client(_sleeper_handler))
    trending = src.trending("add", limit=25)
    assert len(trending) == 4
    assert trending[0].sleeper_id == "6794"
    assert trending[0].count == 32754
    assert trending[0].direction == "add"


def test_sleeper_satisfies_protocol():
    assert isinstance(SleeperSource(), DataSource)


# -- FantasyPros (key-gated ProjectionSource) --------------------------------


def test_fantasypros_off_without_key():
    src = FantasyProsSource()
    assert not src.is_live
    with pytest.raises(SourceNotConfigured):
        src.project()


def test_fantasypros_with_injected_fetch_is_live_and_typed():
    rec = ProjectionRecord(
        source="fantasypros", source_id="16413", source_id_field="fantasypros_id", points=320.5
    )
    src = FantasyProsSource(_fetch=lambda **kw: [rec])
    assert src.is_live
    out = src.project(week=1)
    assert out[0].points == 320.5
    assert isinstance(src, ProjectionSource)


# -- Odds API (implied-total derivation is pure) -----------------------------


def test_odds_implied_totals_math():
    # Favorite (negative spread) gets the larger share: total 48, spread -7 ->
    # 48/2 - (-7)/2 = 24 + 3.5 = 27.5 implied for the favorite.
    games = [{"team": "KCC", "opponent": "DEN", "spread": -7.0, "over_under": 48.0}]
    totals = OddsApiSource.implied_totals(games)
    assert totals[0].team == "KC"
    assert totals[0].opponent == "DEN"
    assert totals[0].implied_total == 27.5


def test_odds_off_without_key():
    src = OddsApiSource()
    assert not src.is_live
    with pytest.raises(SourceNotConfigured):
        src.fetch_implied_totals()


def test_odds_with_injected_fetch():
    src = OddsApiSource(_fetch=lambda **kw: [{"team": "SF", "opponent": "SEA", "spread": -3.0, "over_under": 45.0}])
    assert src.is_live
    totals = src.fetch_implied_totals(week=1)
    assert totals[0].implied_total == 24.0


# -- Open-Meteo (dome logic + not-configured guard) --------------------------


def test_open_meteo_dome_logic():
    src = OpenMeteoSource()
    assert src.is_dome("DET") is True
    assert src.is_dome("GB") is False
    assert not src.is_live  # no fetcher wired by default


def test_open_meteo_requires_fetcher():
    src = OpenMeteoSource()
    with pytest.raises(SourceNotConfigured):
        src.weather_for(["GB"])
