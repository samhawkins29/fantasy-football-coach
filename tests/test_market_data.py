"""The free market layer (P0-2 / P0-4): Sleeper catalog + FFC ADP + store merge.

Fixtures are trimmed captures of the REAL feeds (Aug 2026): Sleeper's
``players/nfl`` blob and FantasyFootballCalculator's PPR/10-team ADP payload —
so the parsers are exercised against genuine field shapes (leading-space gsis
ids, DEF entries keyed by team code, ``PK`` kickers) with zero network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fantasy_coach.ingest.adp import FfcAdpSource, resolve_adp
from fantasy_coach.ingest.catalog import (
    SLEEPER_CATALOG_METHOD,
    SleeperCatalogSource,
    catalog_players,
)
from fantasy_coach.ingest.sources import SleeperSource
from tests.conftest import FIXTURES, make_mock_client

CATALOG_BLOB = json.loads(
    (FIXTURES / "sleeper_catalog_sample.json").read_text(encoding="utf-8")
)
FFC_PAYLOAD = json.loads(
    (FIXTURES / "ffc_adp_sample.json").read_text(encoding="utf-8")
)


# --------------------------------------------------------------------------- #
# Sleeper catalog conversion
# --------------------------------------------------------------------------- #


def test_catalog_keeps_rookies_defs_and_maps_ids():
    players = {p.canonical_id: p for p in catalog_players(CATALOG_BLOB)}

    # Veteran with gsis + yahoo ids: canonical = gsis, both spokes carried.
    flacco = players["00-0026158"]
    assert flacco.name == "Joe Flacco" and flacco.position == "QB"
    assert flacco.ids.yahoo_id == "8795" and flacco.ids.sleeper_id == "19"

    # Rookie with no gsis id yet: SLP_{sleeper_id} canonical — he EXISTS now.
    rookie = players["SLP_13302"]
    assert rookie.position == "RB" and rookie.team == "BAL"

    # Team defense: DST_{team}, flagged as defense.
    eagles = players["DST_PHI"]
    assert eagles.position == "DEF" and eagles.is_defense
    assert "Eagles" in eagles.name

    # Sleeper's messy leading-space gsis ids are stripped; CB → DB group.
    breeland = players["00-0031052"]
    assert breeland.position == "DB"

    assert all(p.resolution_method == SLEEPER_CATALOG_METHOD for p in players.values())


def test_catalog_drops_inactive_teamless_and_offensive_linemen():
    ids = {p.ids.sleeper_id for p in catalog_players(CATALOG_BLOB)}
    assert "2881" not in ids  # inactive
    assert "2801" not in ids  # active but unrostered (no team)
    assert "13940" not in ids  # OL — not a fantasy position


def test_catalog_source_cache_roundtrip(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/players/nfl")
        return httpx.Response(200, json=CATALOG_BLOB)

    source = SleeperCatalogSource(
        sleeper=SleeperSource(client=make_mock_client(handler)),
        cache_dir=tmp_path,
    )
    warmed = source.warm_cache()
    assert (tmp_path / "sleeper_catalog.json").exists()
    loaded = source.load()  # zero network — the mock would fail a re-fetch
    assert {p.canonical_id for p in loaded} == {p.canonical_id for p in warmed}
    by_id = {p.canonical_id: p for p in loaded}
    assert by_id["00-0026158"].ids.yahoo_id == "8795"
    assert by_id["DST_PHI"].is_defense


def test_catalog_load_without_cache_raises(tmp_path):
    with pytest.raises(RuntimeError, match="warm_cache"):
        SleeperCatalogSource(cache_dir=tmp_path).load()


# --------------------------------------------------------------------------- #
# FFC ADP
# --------------------------------------------------------------------------- #


def _ffc(tmp_path, handler=None) -> FfcAdpSource:
    client = make_mock_client(handler) if handler else None
    return FfcAdpSource(scoring_format="ppr", teams=10, cache_dir=tmp_path, client=client)


def test_ffc_fetch_parses_and_caches(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=FFC_PAYLOAD)

    source = _ffc(tmp_path, handler)
    records = source.warm_cache(2026)
    assert "adp/ppr" in seen["url"] and "teams=10" in seen["url"] and "year=2026" in seen["url"]
    by_name = {r.name: r for r in records}
    bijan = by_name["Bijan Robinson"]
    assert bijan.adp == 1.7 and bijan.position == "RB" and bijan.stdev == 0.8
    assert by_name["Seattle Defense"].position == "DEF"
    assert by_name["Seattle Defense"].team == "SEA"
    assert by_name["Brandon Aubrey"].position == "K"  # PK normalized

    # cache-served with zero network afterwards
    loaded = _ffc(tmp_path).load(2026)
    assert {r.name for r in loaded} == {r.name for r in records}


def test_ffc_load_without_cache_raises(tmp_path):
    with pytest.raises(RuntimeError, match="warm_cache"):
        _ffc(tmp_path).load(2026)


def test_resolve_adp_names_defs_and_warnings():
    players = [
        {"canonical_id": "00-B", "name": "Bijan Robinson", "position": "RB", "team": "ATL"},
        {"canonical_id": "00-P", "name": "Puka Nacua", "position": "WR", "team": "LAR"},
        {"canonical_id": "DST_SEA", "name": "Seattle Seahawks", "position": "DEF", "team": "SEA"},
    ]
    from fantasy_coach.ingest.adp import _records_from_payload

    rows, warnings = resolve_adp(_records_from_payload(FFC_PAYLOAD), players)
    by_id = {r["canonical_id"]: r for r in rows}
    assert by_id["00-B"]["average_pick"] == 1.7
    assert by_id["00-P"]["average_pick"] == 3.0
    # The DEF resolves BY TEAM CODE — FFC names it "Seattle Defense".
    assert by_id["DST_SEA"]["average_pick"] == 83.4
    # Everyone we don't know is warned about, never silently dropped.
    assert any("Ja'Marr" in w or "Jaxon" in w or "not in players" in w for w in warnings)


def test_resolve_adp_team_breaks_name_ties():
    players = [
        {"canonical_id": "A", "name": "Josh Allen", "position": "WR", "team": "SEA"},
        {"canonical_id": "B", "name": "Josh Allen", "position": "WR", "team": "DET"},
    ]
    from fantasy_coach.ingest.adp import AdpRecord

    rows, warnings = resolve_adp(
        [AdpRecord(name="Josh Allen", position="WR", team="DET", adp=50.0)], players
    )
    assert rows == [{"canonical_id": "B", "average_pick": 50.0, "stdev": None}]
    assert not warnings


# --------------------------------------------------------------------------- #
# Store merge mode
# --------------------------------------------------------------------------- #


def test_store_merge_fills_ids_without_blanking_richer_fields():
    from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
    from fantasy_coach.store import CoachStore

    store = CoachStore(":memory:")
    # A projection-synthesized row: gsis-keyed, no yahoo/sleeper ids, has a bye.
    prior = CanonicalPlayer(
        canonical_id="00-0026158",
        ids=ExternalIds(gsis_id="00-0026158"),
        name="Joe Flacco",
        position="QB",
        team="IND",  # stale team from history
        bye_week=11,
        resolution_method="projection_meta",
    )
    store.upsert_players([prior])

    merged_in = [p for p in catalog_players(CATALOG_BLOB) if p.canonical_id == "00-0026158"]
    store.upsert_players(merged_in, merge=True)

    row = store.sql("SELECT * FROM players WHERE canonical_id = '00-0026158'")[0]
    assert row["yahoo_id"] == "8795"  # gained — live picks resolve now
    assert row["sleeper_id"] == "19"
    assert row["team"] == "CIN"  # catalog's fresh team wins
    assert row["bye_week"] == 11  # catalog carries no bye — prior kept
    store.close()


def test_store_merge_inserts_rookies_and_defs_as_new_rows():
    from fantasy_coach.store import CoachStore

    store = CoachStore(":memory:")
    store.upsert_players(catalog_players(CATALOG_BLOB), merge=True)
    positions = {r["canonical_id"]: r["position"] for r in store.sql("SELECT * FROM players")}
    assert positions["SLP_13302"] == "RB"  # the rookie exists
    assert positions["DST_PHI"] == "DEF"  # the team defense exists
    store.close()


def test_store_folds_gsis_less_catalog_duplicates():
    # Sleeper's gsis coverage is patchy: a star it can't map arrives as
    # SLP_{id} while the store already holds his gsis row — the fold merges
    # the ids into the canonical row and deletes the duplicate, so ADP/name
    # resolution sees exactly one of him.
    from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
    from fantasy_coach.store import CoachStore

    store = CoachStore(":memory:")
    store.upsert_players([
        CanonicalPlayer(canonical_id="00-STAR", ids=ExternalIds(gsis_id="00-STAR"),
                        name="Bijan Robinson", position="RB", team="ATL",
                        resolution_method="projection_meta"),
    ])
    store.upsert_players([
        CanonicalPlayer(canonical_id="SLP_5850", ids=ExternalIds(sleeper_id="5850", yahoo_id="99999"),
                        name="Bijan Robinson", position="RB", team="ATL",
                        resolution_method="sleeper_catalog"),
    ], merge=True)
    assert store.fold_duplicate_players() == 1
    rows = store.sql("SELECT * FROM players WHERE clean_name = 'bijan robinson'")
    assert len(rows) == 1
    assert rows[0]["canonical_id"] == "00-STAR"
    assert rows[0]["sleeper_id"] == "5850" and rows[0]["yahoo_id"] == "99999"
    store.close()
