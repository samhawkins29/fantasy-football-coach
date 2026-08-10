"""Tests for the SQLite data store: schema/migrations, upserts, queries.

All against throwaway database files (or ``:memory:``) — no network, and every
timestamp goes through an injected clock so vintage assertions are exact.
"""

from __future__ import annotations

import sqlite3

import pytest

from fantasy_coach.clients.models import (
    DraftPick,
    LeagueSettings,
    RosterPosition,
    StatCategory,
)
from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
from fantasy_coach.ingest.projections import PROJECTED_STAT_KEYS
from fantasy_coach.ingest.sources import ProjectionRecord
from fantasy_coach.store import (
    SCHEMA_VERSION,
    STAT_HISTORY_STAT_COLUMNS,
    CoachStore,
    schema_version,
)
from fantasy_coach.value.board import build_value_board

HALF_PPR = {4: 0.04, 5: 4.0, 6: -1.0, 9: 0.1, 10: 6.0, 11: 0.5, 12: 0.1, 13: 6.0, 16: 2.0, 18: -2.0}


def make_settings(league_key: str = "449.l.777", *, max_teams: int = 12) -> LeagueSettings:
    return LeagueSettings(
        league_key=league_key,
        scoring_type="head",
        draft_type="live",
        max_teams=max_teams,
        roster_positions=[
            RosterPosition(position="QB", count=1),
            RosterPosition(position="RB", count=2),
            RosterPosition(position="WR", count=3),
            RosterPosition(position="TE", count=1),
            RosterPosition(position="W/R/T", count=1),
            RosterPosition(position="BN", count=5, is_starting_position=False),
        ],
        stat_categories=[StatCategory(stat_id=sid, value=val) for sid, val in HALF_PPR.items()],
    )


def make_player(
    canonical_id: str,
    name: str,
    position: str,
    *,
    yahoo_id: str | None = None,
    team: str = "",
    bye_week: int | None = None,
    adp: float | None = None,
) -> CanonicalPlayer:
    player = CanonicalPlayer(
        canonical_id=canonical_id,
        ids=ExternalIds(gsis_id=canonical_id, yahoo_id=yahoo_id),
        name=name,
        position=position,
        team=team,
        bye_week=bye_week,
        resolution_method="yahoo_id",
    )
    player.market.adp = adp
    return player


def proj(gsis: str, name: str, position: str, points: float = 0.0, **stats: float) -> ProjectionRecord:
    return ProjectionRecord(
        source="test",
        source_id=gsis,
        source_id_field="gsis_id",
        points=points,
        position=position,
        name=name,
        stats=dict(stats),
    )


@pytest.fixture
def store(tmp_path):
    with CoachStore(tmp_path / "coach.sqlite3", now=lambda: "2026-08-10T00:00:00+00:00") as s:
        yield s


# -- schema / migrations -------------------------------------------------------


def test_open_creates_all_tables_and_records_version(tmp_path):
    store = CoachStore(tmp_path / "coach.sqlite3")
    tables = {
        row["name"]
        for row in store.sql("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "league_settings",
        "players",
        "adp",
        "projections",
        "stats_history",
        "value_board",
        "board_meta",
        "draft_picks",
        "data_vintage",
    } <= tables
    assert schema_version(store.conn) == SCHEMA_VERSION
    store.close()


def test_reopening_is_idempotent_and_preserves_data(tmp_path):
    path = tmp_path / "coach.sqlite3"
    first = CoachStore(path)
    first.upsert_players([make_player("G1", "Keeper", "RB")])
    first.close()

    second = CoachStore(path)  # re-runs apply_migrations on an up-to-date file
    assert schema_version(second.conn) == SCHEMA_VERSION
    assert second.table_counts()["players"] == 1
    second.close()


def test_stats_history_columns_cover_projected_stat_keys():
    # Schema drift guard: if PROJECTED_STAT_KEYS grows, a new migration must
    # widen stats_history (CREATE IF NOT EXISTS won't) — this test is the tripwire.
    assert set(PROJECTED_STAT_KEYS) <= set(STAT_HISTORY_STAT_COLUMNS)


def test_memory_store_works():
    store = CoachStore(":memory:")
    assert store.table_counts()["players"] == 0
    store.close()


# -- league settings round trip ------------------------------------------------


def test_league_settings_round_trip(store):
    settings = make_settings()
    store.upsert_league_settings(settings)
    loaded = store.league_settings("449.l.777")

    assert loaded is not None
    assert loaded.league_key == "449.l.777"
    assert loaded.scoring_type == "head"
    assert loaded.draft_type == "live"
    assert loaded.max_teams == 12
    assert loaded.stat_modifiers() == settings.stat_modifiers()
    assert [(s.position, s.count) for s in loaded.roster_positions] == [
        (s.position, s.count) for s in settings.roster_positions
    ]
    assert loaded.is_superflex == settings.is_superflex is False
    # The bench slot's non-starting flag survives (starter demand depends on it).
    assert loaded.roster_positions[-1].is_starting_position is False


def test_league_settings_upsert_refreshes(store):
    store.upsert_league_settings(make_settings(max_teams=12))
    store.upsert_league_settings(make_settings(max_teams=10))
    assert store.league_settings("449.l.777").max_teams == 10
    assert store.table_counts()["league_settings"] == 1


def test_league_settings_missing_returns_none(store):
    assert store.league_settings("nope") is None


# -- players + ADP -------------------------------------------------------------


def test_players_upsert_refreshes_and_indexes(store):
    store.upsert_players([make_player("G1", "Old Name", "RB", yahoo_id="111", team="KC")])
    store.upsert_players([make_player("G1", "New Name", "RB", yahoo_id="111", team="SF")])
    rows = store.players_by_position("RB")
    assert len(rows) == 1
    assert rows[0]["name"] == "New Name"
    assert rows[0]["team"] == "SF"


def test_fill_only_never_overwrites_richer_rows(store):
    rich = make_player("G1", "Rich Row", "RB", yahoo_id="111", bye_week=9)
    store.upsert_players([rich])
    synthetic = make_player("G1", "Synth Row", "RB")
    new = make_player("G2", "Brand New", "WR")
    written = store.upsert_players([synthetic, new], fill_only=True)
    assert written == 1  # only the new player inserted
    by_id = {r["canonical_id"]: r for r in store.sql("SELECT * FROM players")}
    assert by_id["G1"]["name"] == "Rich Row"
    assert by_id["G1"]["yahoo_id"] == "111"
    assert by_id["G2"]["name"] == "Brand New"


def test_adp_upsert_refreshes_and_keeps_other_players(store):
    store.upsert_adp(
        [
            {"canonical_id": "G1", "average_pick": 5.0, "stdev": 2.0},
            {"canonical_id": "G2", "average_pick": 30.0},
        ],
        source="yahoo",
    )
    # Partial refresh: only G1 comes back — G2's row must survive.
    store.upsert_adp([{"canonical_id": "G1", "average_pick": 3.5, "stdev": 1.5}], source="yahoo")
    rows = {r["canonical_id"]: r for r in store.sql("SELECT * FROM adp")}
    assert rows["G1"]["average_pick"] == 3.5
    assert rows["G2"]["average_pick"] == 30.0


def test_adp_rows_without_pick_are_skipped(store):
    written = store.upsert_adp([{"canonical_id": "G1", "average_pick": None}], source="yahoo")
    assert written == 0
    assert store.table_counts()["adp"] == 0


def test_canonical_players_reattach_stored_adp(store):
    store.upsert_players(
        [make_player("G1", "Alpha", "WR", bye_week=7), make_player("G2", "Beta", "RB")]
    )
    store.upsert_adp([{"canonical_id": "G1", "average_pick": 4.0, "stdev": 1.2}], source="yahoo")
    players = {p.canonical_id: p for p in store.canonical_players()}
    assert players["G1"].market.adp == 4.0
    assert players["G1"].market.adp_stddev == 1.2
    assert players["G1"].bye_week == 7
    assert players["G2"].market.adp is None


def test_canonical_players_pin_adp_source(store):
    store.upsert_players([make_player("G1", "Alpha", "WR")])
    store.upsert_adp([{"canonical_id": "G1", "average_pick": 4.0}], source="sleeper")
    store.upsert_adp([{"canonical_id": "G1", "average_pick": 6.0}], source="yahoo")
    assert store.canonical_players(adp_source="yahoo")[0].market.adp == 6.0
    # No pin: first source alphabetically (sleeper) wins deterministically.
    assert store.canonical_players()[0].market.adp == 4.0


# -- projections ---------------------------------------------------------------


def test_projections_round_trip_and_refresh(store):
    records = [
        proj("G1", "Alpha", "WR", points=200.0, rec_yds=1300.0, rec=90.0, games=17.0),
        proj("G2", "Beta", "RB", points=180.0, rush_yds=1200.0),
    ]
    store.upsert_projections(records, season=2026, note="test vintage")
    loaded = store.projection_records(season=2026)
    assert [r.source_id for r in loaded] == ["G1", "G2"]  # points-desc order
    assert loaded[0].stats == {"rec_yds": 1300.0, "rec": 90.0, "games": 17.0}
    assert loaded[0].source_id_field == "gsis_id"

    store.upsert_projections([proj("G1", "Alpha", "WR", points=150.0)], season=2026)
    assert store.table_counts()["projections"] == 2  # refreshed, not duplicated
    assert store.projection_records(season=2026)[0].source_id == "G2"  # order moved


def test_projection_records_filter_by_season_and_source(store):
    store.upsert_projections([proj("G1", "Alpha", "WR", points=100.0)], season=2025)
    store.upsert_projections([proj("G1", "Alpha", "WR", points=120.0)], season=2026)
    assert len(store.projection_records(season=2025)) == 1
    assert store.projection_records(season=2026)[0].points == 120.0
    assert store.projection_records(source="nope") == []


# -- stats history -------------------------------------------------------------


def test_stats_history_upsert_and_defaults(store):
    rows = [
        {
            "canonical_id": "G1",
            "season": 2025,
            "week": 1,
            "position": "WR",
            "team": "KC",
            "rec": 8.0,
            "rec_yds": 110.0,
            "targets": 11.0,
        }
    ]
    store.upsert_stats_history(rows)
    store.upsert_stats_history(rows)  # idempotent re-run
    stored = store.sql("SELECT * FROM stats_history")
    assert len(stored) == 1
    assert stored[0]["rec_yds"] == 110.0
    assert stored[0]["pass_yds"] == 0.0  # unspecified stat defaults to 0
    assert stored[0]["season_type"] == "REG"


# -- value board snapshot ------------------------------------------------------


def league_board(store):
    settings = make_settings()
    projections = [
        proj("G1", "Alpha Wr", "WR", rec=100.0, rec_yds=1400.0, rec_td=10.0),
        proj("G2", "Beta Rb", "RB", rush_yds=1300.0, rush_td=10.0),
        proj("G3", "Gamma Wr", "WR", rec_yds=700.0, rec=50.0),
        proj("G4", "Delta Rb", "RB", rush_yds=600.0),
    ]
    return settings, build_value_board(projections, settings)


def test_replace_board_round_trip(store):
    settings, board = league_board(store)
    store.upsert_league_settings(settings)
    count = store.replace_board(settings.league_key, board)
    assert count == 4

    stored = store.get_board(settings.league_key)
    assert [r["overall_rank"] for r in stored] == [1, 2, 3, 4]
    by_name = {r["name"]: r for r in stored}
    entry = next(e for e in board.entries if e.name == "Alpha Wr")
    assert by_name["Alpha Wr"]["vorp"] == entry.vorp
    assert by_name["Alpha Wr"]["points"] == entry.points
    assert by_name["Alpha Wr"]["value_source"] == "projection"

    meta = store.board_meta(settings.league_key)
    assert meta["num_teams"] == 12
    assert "WR" in meta["baselines"]


def test_replace_board_is_a_snapshot_not_an_upsert(store):
    settings, board = league_board(store)
    store.upsert_league_settings(settings)
    store.replace_board(settings.league_key, board)

    smaller = build_value_board(
        [proj("G1", "Alpha Wr", "WR", rec_yds=1400.0)], settings
    )
    store.replace_board(settings.league_key, smaller)
    stored = store.get_board(settings.league_key)
    assert len(stored) == 1  # stale entries gone
    assert stored[0]["name"] == "Alpha Wr"


def test_get_board_position_filter_and_limit(store):
    settings, board = league_board(store)
    store.replace_board(settings.league_key, board)
    wrs = store.get_board(settings.league_key, position="WR")
    assert [r["name"] for r in wrs] == ["Alpha Wr", "Gamma Wr"]
    assert len(store.get_board(settings.league_key, limit=2)) == 2


# -- draft picks + available board (the M5 seam) -------------------------------


def test_record_picks_resolves_canonical_and_excludes_from_available(store):
    settings, board = league_board(store)
    store.upsert_league_settings(settings)
    store.replace_board(settings.league_key, board)
    store.upsert_players(
        [
            make_player("G1", "Alpha Wr", "WR", yahoo_id="111"),
            make_player("G2", "Beta Rb", "RB", yahoo_id="222"),
        ]
    )

    picks = [
        DraftPick(pick=1, round=1, team_key="t.1", player_key="449.p.111"),
        DraftPick(pick=2, round=1, team_key="t.2", player_key=""),  # not made yet
    ]
    assert store.record_draft_picks(settings.league_key, picks) == 1
    assert store.drafted_canonical_ids(settings.league_key) == {"G1"}

    available = store.top_available(settings.league_key, 10)
    names = [r["name"] for r in available]
    assert "Alpha Wr" not in names
    assert names[0] == "Beta Rb"


def test_record_picks_upsert_and_unknown_yahoo_id(store):
    settings, board = league_board(store)
    store.replace_board(settings.league_key, board)
    pick = DraftPick(pick=1, round=1, team_key="t.1", player_key="449.p.999")
    store.record_draft_picks(settings.league_key, [pick])
    store.record_draft_picks(settings.league_key, [pick])  # re-poll upserts
    assert store.table_counts()["draft_picks"] == 1
    # Unknown yahoo id -> NULL canonical; the board must NOT be wiped by the
    # exclusion subquery (the NOT IN + NULL trap).
    assert store.drafted_canonical_ids(settings.league_key) == set()
    assert len(store.top_available(settings.league_key, 10)) == 4


def test_top_available_position_filter(store):
    settings, board = league_board(store)
    store.replace_board(settings.league_key, board)
    rbs = store.top_available(settings.league_key, 10, position="RB")
    assert [r["name"] for r in rbs] == ["Beta Rb", "Delta Rb"]


# -- analysis reads ------------------------------------------------------------


def test_adp_vs_vorp_gaps(store):
    settings = make_settings()
    projections = [
        proj("G1", "Alpha", "WR", rec_yds=1400.0),
        proj("G2", "Beta", "WR", rec_yds=1000.0),
        proj("G3", "Gamma", "WR", rec_yds=600.0),
    ]
    players = [
        make_player("G1", "Alpha", "WR", adp=20.0),  # market sleeps on our #1
        make_player("G2", "Beta", "WR", adp=1.0),  # market reaches
        make_player("G3", "Gamma", "WR"),  # no ADP -> excluded
    ]
    board = build_value_board(projections, settings, players=players)
    store.replace_board(settings.league_key, board)

    gaps = store.adp_vs_vorp(settings.league_key)
    assert [r["name"] for r in gaps] == ["Alpha", "Beta"]
    assert gaps[0]["gap"] == pytest.approx(19.0)  # adp 20 - rank 1
    assert gaps[1]["gap"] == pytest.approx(-1.0)  # adp 1 - rank 2
    assert store.adp_vs_vorp(settings.league_key, limit=1)[0]["name"] == "Alpha"


def test_player_summary_by_id_and_name(store):
    store.upsert_players([make_player("G1", "Amon-Ra St. Brown", "WR", team="DET")])
    store.upsert_adp([{"canonical_id": "G1", "average_pick": 8.0}], source="yahoo")
    store.upsert_projections([proj("G1", "Amon-Ra St. Brown", "WR", points=250.0)], season=2026)
    store.upsert_stats_history(
        [{"canonical_id": "G1", "season": 2025, "week": 1, "rec": 9.0}]
    )

    for term in ("G1", "amon-ra", "St. Brown"):
        summary = store.player_summary(term)
        assert summary is not None, term
        assert summary["player"]["canonical_id"] == "G1"
        assert summary["adp"][0]["average_pick"] == 8.0
        assert summary["projections"][0]["points"] == 250.0
        assert summary["stats_history"][0]["rec"] == 9.0

    assert store.player_summary("Nobody Realmuto") is None


def test_raw_sql_works_against_the_file(store):
    settings, board = league_board(store)
    store.replace_board(settings.league_key, board)
    rows = store.sql(
        "SELECT position, COUNT(*) AS n, MAX(vorp) AS best FROM value_board "
        "WHERE league_key = ? GROUP BY position ORDER BY position",
        (settings.league_key,),
    )
    assert [(r["position"], r["n"]) for r in rows] == [("RB", 2), ("WR", 2)]
    # And with plain sqlite3, no CoachStore at all — it's just a file.
    conn = sqlite3.connect(store.path)
    assert conn.execute("SELECT COUNT(*) FROM value_board").fetchone()[0] == 4
    conn.close()


def test_vintage_stamps_track_refreshes(store):
    store.upsert_players([make_player("G1", "Alpha", "WR")])
    store.upsert_adp([{"canonical_id": "G1", "average_pick": 2.0}], source="yahoo")
    scopes = {row["scope"] for row in store.vintage()}
    assert {"players", "adp:yahoo"} <= scopes
    assert all(row["refreshed_at"] == "2026-08-10T00:00:00+00:00" for row in store.vintage())
