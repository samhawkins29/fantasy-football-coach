"""Tests for the canonical player model (M3, framework §3.3)."""

from __future__ import annotations

from fantasy_coach.ingest.canonical import (
    CanonicalPlayer,
    ExternalIds,
    dst_canonical_id,
    unresolved_canonical_id,
)


def test_synthetic_ids():
    assert dst_canonical_id("SFO") == "DST_SF"     # team normalized
    assert dst_canonical_id("KC") == "DST_KC"
    assert unresolved_canonical_id("45001") == "UNK_45001"


def test_external_ids_merge_missing_only_fills_gaps():
    primary = ExternalIds(gsis_id="00-1", yahoo_id=None, sleeper_id="s1")
    secondary = ExternalIds(gsis_id="00-BAD", yahoo_id="y2", sleeper_id="s-BAD", espn_id="e2")
    primary.merge_missing(secondary)
    assert primary.gsis_id == "00-1"      # not overwritten
    assert primary.sleeper_id == "s1"     # not overwritten
    assert primary.yahoo_id == "y2"       # gap filled
    assert primary.espn_id == "e2"        # gap filled


def test_canonical_player_post_init_normalizes():
    p = CanonicalPlayer(canonical_id="00-1", name="Amon-Ra St. Brown", position="wr", team="SFO")
    assert p.clean_name == "amon ra st brown"
    assert p.position == "WR"
    assert p.team == "SF"
    assert p.is_resolved


def test_unresolved_player_flag():
    p = CanonicalPlayer(canonical_id="UNK_9", name="Rookie X")
    assert not p.is_resolved


def test_for_defense_constructor():
    d = CanonicalPlayer.for_defense("SFO")
    assert d.canonical_id == "DST_SF"
    assert d.is_defense
    assert d.position == "DEF"
    assert d.team == "SF"
    assert d.name == "SF DST"
    assert d.is_resolved  # defenses are intentionally synthetic, still "resolved"


def test_attached_slots_default_empty():
    p = CanonicalPlayer(canonical_id="00-1")
    # M4's join targets exist and are empty until the value engine fills them.
    assert p.projections.blended is None
    assert p.projections.by_source == {}
    assert p.opportunity.snap_pct is None
    assert p.environment.implied_team_total is None
    assert p.market.adp is None
    assert p.value.vorp is None
