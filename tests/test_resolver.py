"""Tests for the IdResolver pipeline — the critical seam (M3, framework §3.2)."""

from __future__ import annotations

from fantasy_coach.ingest.resolver import (
    METHOD_DETERMINISTIC,
    METHOD_DST,
    METHOD_FUZZY,
    METHOD_OVERRIDE,
    METHOD_UNMATCHED,
    METHOD_YAHOO_ID,
    IdResolver,
    load_overrides,
)

from .conftest import make_identity


# -- stage 3: direct yahoo_id (happy path) ----------------------------------


def test_direct_yahoo_id_match(crosswalk):
    resolver = IdResolver(crosswalk)
    ident = make_identity(yahoo_player_id="30123", full_name="Patrick Mahomes", team_abbr="KC", position="QB")
    res = resolver.resolve(ident)
    assert res.method == METHOD_YAHOO_ID
    assert res.canonical_id == "00-0033873"
    assert res.confidence == 100.0
    assert res.matched


# -- stage 4: deterministic (name, pos, team) -------------------------------


def test_deterministic_match_when_yahoo_id_absent(crosswalk):
    # Ken Walker III has no yahoo_id in the map; Yahoo sends id 34123 (unknown to
    # the crosswalk) but name/pos/team resolve deterministically.
    resolver = IdResolver(crosswalk)
    ident = make_identity(yahoo_player_id="34123", full_name="Ken Walker III", team_abbr="SEA", position="RB")
    res = resolver.resolve(ident)
    assert res.method == METHOD_DETERMINISTIC
    assert res.canonical_id == "00-0037746"


def test_deterministic_handles_team_code_mismatch(crosswalk):
    # Yahoo 'SF' vs crosswalk 'SFO' — both normalize to SF, so this matches even
    # with an unknown yahoo id.
    resolver = IdResolver(crosswalk)
    ident = make_identity(yahoo_player_id="99", full_name="Christian McCaffrey", team_abbr="SF", position="RB")
    res = resolver.resolve(ident)
    assert res.method in (METHOD_YAHOO_ID, METHOD_DETERMINISTIC)
    assert res.canonical_id == "00-0033280"


# -- stage 5: fuzzy ----------------------------------------------------------


def test_fuzzy_match_within_bucket_real_rapidfuzz(crosswalk):
    # Slight spelling drift ("Amon Ra St Brown" w/o hyphen), unknown yahoo id,
    # correct pos+team bucket -> fuzzy resolves via real rapidfuzz.
    resolver = IdResolver(crosswalk)
    ident = make_identity(yahoo_player_id="88", full_name="Amonra St. Browne", team_abbr="DET", position="WR")
    res = resolver.resolve(ident)
    assert res.method == METHOD_FUZZY
    assert res.canonical_id == "00-0036963"
    assert 87.0 <= res.confidence <= 100.0
    assert res.needs_review


def test_fuzzy_below_threshold_is_unmatched():
    # An injected scorer that always returns a low score forces no fuzzy accept.
    from fantasy_coach.ingest.crosswalk import load_id_crosswalk

    cw = load_id_crosswalk(
        rows=[{"name": "Real Guy", "position": "WR", "team": "DET", "gsis_id": "00-9", "yahoo_id": 1}]
    )
    resolver = IdResolver(cw, fuzzy_scorer=lambda a, b: 10.0)
    ident = make_identity(yahoo_player_id="2", full_name="Totally Different", team_abbr="DET", position="WR")
    res = resolver.resolve(ident)
    assert res.method == METHOD_UNMATCHED
    assert res.canonical_id == "UNK_2"


def test_fuzzy_scorer_is_injectable_and_bucketed():
    from fantasy_coach.ingest.crosswalk import load_id_crosswalk

    cw = load_id_crosswalk(
        rows=[
            {"name": "Aaa Bbb", "position": "WR", "team": "NE", "gsis_id": "00-1", "yahoo_id": 1},
            {"name": "Ccc Ddd", "position": "RB", "team": "NE", "gsis_id": "00-2", "yahoo_id": 2},
        ]
    )
    # Scorer that returns 95 for everything: only the WR/NE bucket candidate is
    # eligible for a WR/NE query, so it can't leak the RB.
    resolver = IdResolver(cw, fuzzy_scorer=lambda a, b: 95.0)
    ident = make_identity(yahoo_player_id="9", full_name="Zzz Yyy", team_abbr="NE", position="WR")
    res = resolver.resolve(ident)
    assert res.method == METHOD_FUZZY
    assert res.canonical_id == "00-1"


# -- stage 2: DST ------------------------------------------------------------


def test_defense_maps_by_team_code(crosswalk):
    resolver = IdResolver(crosswalk)
    ident = make_identity(
        yahoo_player_id="100014", full_name="San Francisco", team_abbr="SFO", position="DEF", position_type="DT"
    )
    res = resolver.resolve(ident)
    assert res.method == METHOD_DST
    assert res.canonical_id == "DST_SF"
    assert res.row is None


def test_defense_short_circuits_before_id_join(crosswalk):
    # Even if a defense had a yahoo id present in the map, is_defense wins.
    resolver = IdResolver(crosswalk)
    ident = make_identity(yahoo_player_id="30123", full_name="KC", team_abbr="KC", position="DEF")
    res = resolver.resolve(ident)
    assert res.method == METHOD_DST
    assert res.canonical_id == "DST_KC"


# -- stage 1: override -------------------------------------------------------


def test_override_wins_over_everything(crosswalk):
    # Force Mahomes' yahoo id to resolve to a different gsis via override.
    resolver = IdResolver(crosswalk, overrides={"30123": "00-OVERRIDE"})
    ident = make_identity(yahoo_player_id="30123", full_name="Patrick Mahomes", team_abbr="KC", position="QB")
    res = resolver.resolve(ident)
    assert res.method == METHOD_OVERRIDE
    assert res.canonical_id == "00-OVERRIDE"


def test_load_overrides_from_records():
    ov = load_overrides([{"yahoo_id": "111", "gsis_id": "00-A"}, {"yahoo_id": "", "gsis_id": "bad"}])
    assert ov == {"111": "00-A"}


def test_add_override_at_runtime(crosswalk):
    resolver = IdResolver(crosswalk)
    resolver.add_override("34123", "00-CUSTOM")
    ident = make_identity(yahoo_player_id="34123", full_name="Ken Walker III", team_abbr="SEA", position="RB")
    assert resolver.resolve(ident).canonical_id == "00-CUSTOM"


# -- stage 6: unmatched ------------------------------------------------------


def test_unmatched_rookie(crosswalk):
    # A brand-new rookie: no yahoo id, no name match, empty bucket.
    resolver = IdResolver(crosswalk)
    ident = make_identity(yahoo_player_id="45001", full_name="Freshman Rookie", team_abbr="NYJ", position="WR")
    res = resolver.resolve(ident)
    assert res.method == METHOD_UNMATCHED
    assert res.canonical_id == "UNK_45001"
    assert not res.matched


# -- batch report ------------------------------------------------------------


def test_resolve_all_report(crosswalk):
    resolver = IdResolver(crosswalk)
    idents = [
        make_identity(yahoo_player_id="30123", full_name="Patrick Mahomes", team_abbr="KC", position="QB"),
        make_identity(yahoo_player_id="34123", full_name="Ken Walker III", team_abbr="SEA", position="RB"),
        make_identity(yahoo_player_id="99", full_name="Amonra St. Browne", team_abbr="DET", position="WR"),
        make_identity(yahoo_player_id="100014", full_name="SF", team_abbr="SF", position="DEF"),
        make_identity(yahoo_player_id="45001", full_name="Freshman Rookie", team_abbr="NYJ", position="WR"),
    ]
    report = resolver.resolve_all(idents)
    summary = report.summary()
    assert summary["total"] == 5
    assert summary["matched"] == 4
    assert summary["unmatched"] == 1
    assert summary["needs_review"] == 1  # the fuzzy Amon-Ra
    assert summary["by_method"][METHOD_DST] == 1
    assert report.unmatched[0].yahoo_player_id == "45001"
    assert report.needs_review[0].method == METHOD_FUZZY
    assert 0.0 < report.match_rate <= 1.0
