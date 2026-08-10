"""Tests for league-settings-driven rescoring (M4 §4.1 step 1).

The invariant under test: point totals come from the league's own
``stat_modifiers``, never from a hardcoded scoring system — PPR type, passing-TD
value, and turnover penalties all flow from the :class:`LeagueSettings` object.
"""

from __future__ import annotations

import pytest

from fantasy_coach.clients.models import LeagueSettings, StatCategory
from fantasy_coach.ingest.projections import PROJECTED_STAT_KEYS, REFERENCE_SCORING
from fantasy_coach.value.scoring import YAHOO_STAT_KEYS, league_points, league_scoring


def settings_with(scoring: dict[int, float]) -> LeagueSettings:
    return LeagueSettings(
        stat_categories=[StatCategory(stat_id=sid, value=val) for sid, val in scoring.items()]
    )


#: A full-PPR, 4-pt-pass-TD league's modifiers, keyed by Yahoo stat id.
FULL_PPR = {4: 0.04, 5: 4.0, 6: -1.0, 9: 0.1, 10: 6.0, 11: 1.0, 12: 0.1, 13: 6.0, 16: 2.0, 18: -2.0}


def test_league_scoring_maps_every_yahoo_stat_id():
    scoring = league_scoring(settings_with(FULL_PPR))
    assert scoring == {
        "pass_yds": 0.04,
        "pass_td": 4.0,
        "pass_int": -1.0,
        "rush_yds": 0.1,
        "rush_td": 6.0,
        "rec": 1.0,
        "rec_yds": 0.1,
        "rec_td": 6.0,
        "two_pt": 2.0,
        "fum_lost": -2.0,
    }


def test_league_scoring_ignores_stats_projections_cannot_cover():
    # Return TDs (15) and a made-up IDP stat have no projected component — they
    # must be skipped, not crash or leak unknown keys into the map.
    scoring = league_scoring(settings_with({11: 0.5, 15: 6.0, 78: 2.0}))
    assert scoring == {"rec": 0.5}


def test_league_scoring_partial_settings_only_carry_scored_stats():
    scoring = league_scoring(settings_with({9: 0.1, 10: 6.0}))
    assert scoring == {"rush_yds": 0.1, "rush_td": 6.0}


def test_league_scoring_empty_settings_fall_back_to_reference():
    assert league_scoring(LeagueSettings()) == REFERENCE_SCORING


def test_ppr_type_changes_points_for_the_same_stat_line():
    stats = {"rec": 100.0, "rec_yds": 1000.0}
    standard = league_points(stats, settings_with(FULL_PPR | {11: 0.0}))
    half = league_points(stats, settings_with(FULL_PPR | {11: 0.5}))
    full = league_points(stats, settings_with(FULL_PPR))
    assert standard == pytest.approx(100.0)
    assert half == pytest.approx(150.0)
    assert full == pytest.approx(200.0)


def test_six_point_passing_td_league_flows_through():
    stats = {"pass_td": 30.0}
    four = league_points(stats, settings_with(FULL_PPR))
    six = league_points(stats, settings_with(FULL_PPR | {5: 6.0}))
    assert four == pytest.approx(120.0)
    assert six == pytest.approx(180.0)


def test_te_premium_style_reception_value_flows_through():
    # A league scoring receptions at 1.5 (TE-premium-style custom value) must
    # produce exactly that per-catch value — no constant anywhere.
    stats = {"rec": 80.0}
    assert league_points(stats, settings_with({11: 1.5})) == pytest.approx(120.0)


def test_yahoo_stat_keys_target_only_projected_components():
    assert set(YAHOO_STAT_KEYS.values()) <= set(PROJECTED_STAT_KEYS)
