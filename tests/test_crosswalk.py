"""Tests for the id crosswalk loader + indices (M3, framework §3.1–3.2)."""

from __future__ import annotations

from fantasy_coach.ingest.crosswalk import (
    IdCrosswalk,
    clean_source_id,
    load_id_crosswalk,
)


# -- id coercion (the float/NaN quirk) --------------------------------------


def test_clean_source_id_handles_float_int_str_nan():
    assert clean_source_id(32692.0) == "32692"        # float w/ .0 dropped
    assert clean_source_id(32692) == "32692"          # int
    assert clean_source_id("00-0036322") == "00-0036322"  # gsis string kept
    assert clean_source_id("32692.0") == "32692"      # stringified float
    assert clean_source_id(float("nan")) is None      # NaN -> None
    assert clean_source_id(None) is None
    assert clean_source_id("") is None
    assert clean_source_id("nan") is None


# -- loading from fixture records -------------------------------------------


def test_load_from_rows_normalizes_and_indexes(crosswalk: IdCrosswalk):
    assert len(crosswalk) == 7
    row = crosswalk.by_yahoo_id("30123")  # Mahomes, from yahoo_id 30123.0
    assert row is not None
    assert row.gsis_id == "00-0033873"
    assert row.team == "KC"            # KCC normalized
    assert row.ids["sleeper_id"] == "4046"
    assert row.ids["espn_id"] == "3139477"


def test_missing_yahoo_id_not_directly_joinable(crosswalk: IdCrosswalk):
    # Ken Walker + Caleb Williams have NaN yahoo_id in the fixture.
    assert crosswalk.by_yahoo_id("does-not-exist") is None
    # But they ARE reachable by their (name, pos, team) tuple.
    row = crosswalk.by_match_key(("ken walker", "RB", "SEA"))
    assert row is not None and row.gsis_id == "00-0037746"


def test_duplicate_name_is_a_collision_not_a_silent_pick(crosswalk: IdCrosswalk):
    # Two "Justin Jefferson" rows: WR/MIN (real) and LB/CLE (old). Different
    # (name,pos,team) keys, so each resolves uniquely...
    wr = crosswalk.by_match_key(("justin jefferson", "WR", "MIN"))
    assert wr is not None and wr.gsis_id == "00-0036322"
    lb = crosswalk.by_match_key(("justin jefferson", "LB", "CLE"))
    assert lb is not None and lb.gsis_id is None


def test_ambiguous_key_returns_none():
    # Same name+pos+team on two different players -> ambiguous -> None (caller
    # must fall through rather than guess).
    rows = [
        {"name": "John Smith", "position": "WR", "team": "NE", "gsis_id": "00-1", "yahoo_id": 1},
        {"name": "John Smith", "position": "WR", "team": "NE", "gsis_id": "00-2", "yahoo_id": 2},
    ]
    cw = load_id_crosswalk(rows=rows)
    assert cw.by_match_key(("john smith", "WR", "NE")) is None
    collisions = cw.collisions()
    assert ("john smith", "WR", "NE") in collisions
    assert len(collisions[("john smith", "WR", "NE")]) == 2


def test_candidates_in_bucket(crosswalk: IdCrosswalk):
    bucket = crosswalk.candidates_in_bucket("WR", "MIN")
    assert [r.name for r in bucket] == ["Justin Jefferson"]
    assert crosswalk.candidates_in_bucket("QB", "KC")[0].name == "Patrick Mahomes"


# -- Sleeper gap-fill (§3.1 "second yahoo source") --------------------------


def test_gap_fill_from_sleeper_fills_missing_yahoo_id(crosswalk: IdCrosswalk, ):
    # Ken Walker (sleeper_id 8151) has NO yahoo_id from DynastyProcess; Sleeper
    # provides yahoo_id 34123. After gap-fill it becomes directly joinable.
    assert crosswalk.by_yahoo_id("34123") is None
    sleeper_players = {
        "8151": {"yahoo_id": 34123, "gsis_id": "00-0037746"},
        "11560": {"yahoo_id": 40155, "gsis_id": "00-0039918"},
    }
    filled = crosswalk.gap_fill_from_sleeper(sleeper_players)
    assert filled >= 2
    row = crosswalk.by_yahoo_id("34123")
    assert row is not None and row.name == "Ken Walker III"


def test_gap_fill_never_overwrites_primary(crosswalk: IdCrosswalk):
    # Mahomes already has yahoo_id 30123; a bogus Sleeper value must not win.
    sleeper_players = {"4046": {"yahoo_id": 99999}}
    crosswalk.gap_fill_from_sleeper(sleeper_players)
    row = crosswalk.by_source_id("gsis_id", "00-0033873")
    assert row.ids["yahoo_id"] == "30123"  # unchanged


# -- DataFrame-like path -----------------------------------------------------


class _FakeDataFrame:
    """Minimal stand-in exposing to_dict('records') like pandas."""

    def __init__(self, records):
        self._records = records

    def to_dict(self, orient):
        assert orient == "records"
        return self._records


def test_load_from_dataframe_like_fetch():
    df = _FakeDataFrame(
        [{"name": "Josh Allen", "position": "QB", "team": "BUF", "gsis_id": "00-0034857", "yahoo_id": 30977.0}]
    )
    cw = load_id_crosswalk(fetch=lambda: df)
    row = cw.by_yahoo_id("30977")
    assert row is not None and row.team == "BUF"
