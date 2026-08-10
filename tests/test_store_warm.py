"""Tests for the store warm path: end-to-end population + offline degradation.

The warm pass is exercised exactly as draft prep would run it — once "online"
(full inputs) and again "offline" (failing projection source, no players, no
stats) — asserting that the second pass degrades to warnings while prior rows
and the ADP-driven board entries survive.
"""

from __future__ import annotations

import pytest

from fantasy_coach.clients.models import LeagueSettings, RosterPosition, StatCategory
from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
from fantasy_coach.ingest.projections import PROJECTION_NOTE
from fantasy_coach.ingest.sources import ProjectionRecord
from fantasy_coach.store import CoachStore, stats_rows_from_nflverse, warm_store
from fantasy_coach.store.warm import SYNTHESIZED_FROM_PROJECTIONS

HALF_PPR = {4: 0.04, 5: 4.0, 6: -1.0, 9: 0.1, 10: 6.0, 11: 0.5, 12: 0.1, 13: 6.0, 16: 2.0, 18: -2.0}
LEAGUE_KEY = "449.l.777"


def make_settings() -> LeagueSettings:
    return LeagueSettings(
        league_key=LEAGUE_KEY,
        scoring_type="head",
        max_teams=12,
        roster_positions=[
            RosterPosition(position="QB", count=1),
            RosterPosition(position="RB", count=2),
            RosterPosition(position="WR", count=2),
            RosterPosition(position="W/R/T", count=1),
            RosterPosition(position="BN", count=5, is_starting_position=False),
        ],
        stat_categories=[StatCategory(stat_id=sid, value=val) for sid, val in HALF_PPR.items()],
    )


def proj(gsis: str, name: str, position: str, **stats: float) -> ProjectionRecord:
    return ProjectionRecord(
        source="nflverse_model",
        source_id=gsis,
        source_id_field="gsis_id",
        points=0.0,
        position=position,
        team="KC",
        name=name,
        stats=dict(stats),
    )


def projection_set() -> list[ProjectionRecord]:
    return [
        proj("G1", "Alpha Wr", "WR", rec=100.0, rec_yds=1400.0, rec_td=10.0),
        proj("G2", "Beta Rb", "RB", rush_yds=1300.0, rush_td=10.0),
        proj("G3", "Gamma Wr", "WR", rec_yds=800.0, rec=60.0),
        proj("G4", "Delta Rb", "RB", rush_yds=700.0),
        proj("G5", "Epsilon Qb", "QB", pass_yds=4500.0, pass_td=35.0),
    ]


def make_player(canonical_id: str, name: str, position: str, *, adp: float | None = None) -> CanonicalPlayer:
    player = CanonicalPlayer(
        canonical_id=canonical_id,
        ids=ExternalIds(gsis_id=canonical_id, yahoo_id=f"y{canonical_id}"),
        name=name,
        position=position,
        resolution_method="yahoo_id",
    )
    player.market.adp = adp
    return player


def player_set() -> list[CanonicalPlayer]:
    return [
        make_player("G1", "Alpha Wr", "WR", adp=2.0),
        make_player("G2", "Beta Rb", "RB", adp=5.0),
        make_player("G3", "Gamma Wr", "WR", adp=25.0),
        make_player("G4", "Delta Rb", "RB", adp=40.0),
        make_player("G5", "Epsilon Qb", "QB", adp=30.0),
        make_player("R1", "Hot Rookie", "WR", adp=18.0),  # no projection -> ADP entry
    ]


class ExplodingSource:
    """A ProjectionSource whose pull always fails (the offline case)."""

    name = "exploding"
    is_live = True

    def project(self, *, week=None, season=None):
        raise RuntimeError("network down")


NFLVERSE_ROWS = [
    {
        "player_id": "G1",
        "season": 2025,
        "week": 1,
        "season_type": "REG",
        "position": "WR",
        "team": "KC",  # 2025 schema vintage
        "receptions": 8.0,
        "receiving_yards": 120.0,
        "receiving_tds": 1.0,
        "targets": 11.0,
        "passing_interceptions": 0.0,
    },
    {
        "player_id": "G2",
        "season": 2024,
        "week": 3,
        "position": "RB",
        "recent_team": "SFO",  # pre-2025 schema vintage
        "rushing_yards": 90.0,
        "carries": 18.0,
        "interceptions": 0.0,
        "rushing_fumbles_lost": 1.0,
    },
    {"season": 2024, "week": 1, "rushing_yards": 50.0},  # no player_id -> dropped
]


@pytest.fixture
def store(tmp_path):
    with CoachStore(tmp_path / "coach.sqlite3") as s:
        yield s


def warm_full(store) -> object:
    return warm_store(
        store,
        make_settings(),
        projections=projection_set(),
        players=player_set(),
        stats_rows=NFLVERSE_ROWS,
        season=2026,
    )


# -- the full (online) pass ----------------------------------------------------


def test_full_warm_populates_every_table(store):
    result = warm_full(store)
    assert result.league_key == LEAGUE_KEY
    assert result.season == 2026
    assert result.counts["league_settings"] == 1
    assert result.counts["players"] == 6
    assert result.counts["adp"] == 6
    assert result.counts["projections"] == 5
    assert result.counts["stats_history"] == 2
    # 5 projected + 1 ADP-only rookie on the board.
    assert result.counts["value_board"] == 6
    assert result.board_entries == 6
    assert result.warnings == []
    assert set(result.refreshed) == {
        "league_settings",
        "projections",
        "players",
        "adp:yahoo",
        "stats_history",
        "value_board",
    }
    assert "6 rows" in result.summary()


def test_full_warm_board_carries_adp_and_gap_fill(store):
    warm_full(store)
    board = store.get_board(LEAGUE_KEY)
    by_name = {r["name"]: r for r in board}
    assert by_name["Alpha Wr"]["value_source"] == "projection"
    assert by_name["Alpha Wr"]["adp"] == 2.0  # stored ADP joined onto the entry
    assert by_name["Hot Rookie"]["value_source"] == "adp"
    assert by_name["Hot Rookie"]["points"] is None


def test_full_warm_stamps_projection_note_and_vintage(store):
    warm_full(store)
    row = store.sql("SELECT note FROM projections LIMIT 1")[0]
    assert row["note"] == PROJECTION_NOTE
    scopes = {r["scope"] for r in store.vintage()}
    assert f"value_board:{LEAGUE_KEY}" in scopes
    assert "projections:nflverse_model:2026" in scopes


def test_settings_round_trip_through_warm(store):
    warm_full(store)
    loaded = store.league_settings(LEAGUE_KEY)
    assert loaded.max_teams == 12
    assert loaded.stat_modifiers() == make_settings().stat_modifiers()


def test_projection_source_is_used_when_no_records_given(store):
    class CannedSource:
        name = "nflverse_model"

        def project(self, *, week=None, season=None):
            assert season == 2026
            return projection_set()

    result = warm_store(
        store, make_settings(), projection_source=CannedSource(), season=2026
    )
    assert result.counts["projections"] == 5
    assert result.counts["value_board"] == 5


# -- re-runs refresh, offline degrades ----------------------------------------


def test_rewarm_refreshes_rather_than_duplicates(store):
    warm_full(store)
    result = warm_full(store)
    assert result.counts["players"] == 6
    assert result.counts["projections"] == 5
    assert result.counts["value_board"] == 6
    assert result.counts["stats_history"] == 2


def test_offline_rewarm_keeps_prior_rows_and_adp_board(store):
    warm_full(store)  # the online pass persisted players + ADP

    result = warm_store(
        store,
        make_settings(),
        projection_source=ExplodingSource(),
        season=2026,
    )
    # Degraded but not broken: warnings name each missing input...
    assert any("projection pull failed" in w for w in result.warnings)
    assert any("no canonical players" in w for w in result.warnings)
    assert any("stats history" in w for w in result.warnings)
    # ...prior rows all survive...
    assert result.counts["players"] == 6
    assert result.counts["adp"] == 6
    assert result.counts["projections"] == 5
    assert result.counts["stats_history"] == 2
    # ...and the board still rebuilt from STORED projections + STORED ADP,
    # including the ADP-only rookie (the reason adp is persisted at all).
    assert result.counts["value_board"] == 6
    rookie = next(r for r in store.get_board(LEAGUE_KEY) if r["name"] == "Hot Rookie")
    assert rookie["value_source"] == "adp"
    assert "value_board" in result.refreshed
    assert "players" not in result.refreshed


def test_cold_offline_warm_synthesizes_players_from_projections(store):
    # First-ever warm with no Yahoo session at all: projections only.
    result = warm_store(
        store, make_settings(), projections=projection_set(), season=2026
    )
    assert result.counts["players"] == 5  # synthesized identity rows
    row = store.sql("SELECT resolution_method FROM players LIMIT 1")[0]
    assert row["resolution_method"] == SYNTHESIZED_FROM_PROJECTIONS
    assert result.counts["adp"] == 0
    assert result.counts["value_board"] == 5  # projection entries, no gap-fill
    assert any("synthesized" in w for w in result.warnings)


def test_synthesized_players_never_downgrade_crosswalked_rows(store):
    warm_full(store)
    warm_store(store, make_settings(), projections=projection_set(), season=2026)
    alpha = store.sql("SELECT * FROM players WHERE canonical_id = 'G1'")[0]
    assert alpha["yahoo_id"] == "yG1"  # the rich row survived the fill-only pass
    assert alpha["resolution_method"] == "yahoo_id"


def test_cold_warm_with_nothing_at_all_warns_and_stores_settings(store):
    result = warm_store(store, make_settings(), season=2026)
    assert result.counts["league_settings"] == 1
    assert result.counts["value_board"] == 0
    assert result.board_entries == 0
    assert any("board not rebuilt" in w for w in result.warnings)


def test_players_without_adp_warns_but_stores_them(store):
    players = [make_player("G1", "Alpha Wr", "WR")]  # attach_yahoo_market never ran
    result = warm_store(
        store,
        make_settings(),
        projections=projection_set(),
        players=players,
        season=2026,
    )
    assert result.counts["players"] == 1
    assert result.counts["adp"] == 0
    assert any("no ADP" in w for w in result.warnings)


# -- nflverse row shaping ------------------------------------------------------


def test_stats_rows_from_nflverse_handles_both_schema_vintages():
    shaped = stats_rows_from_nflverse(NFLVERSE_ROWS)
    assert len(shaped) == 2  # the player_id-less row dropped
    g1, g2 = shaped
    assert g1["canonical_id"] == "G1"
    assert g1["team"] == "KC"
    assert g1["rec"] == 8.0
    assert g1["rec_yds"] == 120.0
    assert g1["targets"] == 11.0
    assert g2["team"] == "SF"  # SFO alias normalized
    assert g2["carries"] == 18.0
    assert g2["fum_lost"] == 1.0  # coalesced from rushing_fumbles_lost
    assert g2["season_type"] == "REG"  # default when the column is absent


def test_stats_rows_accept_dataframe_like_objects():
    class FrameLike:
        def to_dict(self, orient):
            assert orient == "records"
            return [dict(NFLVERSE_ROWS[0])]

    shaped = stats_rows_from_nflverse(FrameLike())
    assert len(shaped) == 1 and shaped[0]["canonical_id"] == "G1"
