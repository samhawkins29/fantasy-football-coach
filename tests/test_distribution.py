"""Projection distributions (upgrade 1): floor / median / ceiling.

Invariants under test:

* the spread model orders players by *relative* risk the way the data says
  it should: boom-bust weekly history → wider spread than steady history;
  a thin sample (projection built mostly from the positional prior) → wider
  than an established one; more projected games → tighter (√G);
* floor ≤ median ≤ ceiling, floor never negative, everything finite;
* the nflverse source emits floor/ceiling on every record and they survive
  the JSON cache round trip;
* the board re-centres the spread on *league* points as ratios (floor/ceiling
  ratios are invariant to scoring) and brackets VORP the same way;
* ``risk_preference=0`` is the identity on ranking; ``>0`` lifts the wider-
  ceiling player relative to a same-median steady one, ``<0`` the reverse; the
  tilt is monotone in the dial;
* the consensus blend carries the spread through and widens it when its
  signals disagree;
* the recommendation narrates the range and the bet type.
"""

from __future__ import annotations

import math

import pytest

from fantasy_coach.clients.models import LeagueSettings, RosterPosition, StatCategory
from fantasy_coach.ingest.projections import NflverseProjectionSource
from fantasy_coach.ingest.sources import NflverseSource, ProjectionRecord
from fantasy_coach.ingest.variance import (
    POSITION_CV_PRIOR,
    SpreadModel,
    positional_cv_priors,
    spread_ratios,
    weekly_cv,
    widen_for_disagreement,
)
from fantasy_coach.value.board import build_value_board
from fantasy_coach.draft.recommend import (
    build_recommendation,
    compute_needs,
    distribution_note,
    rank_available,
    roster_slots,
)


# -- the spread model ---------------------------------------------------------


def test_weekly_cv_basic_and_degenerate():
    assert weekly_cv([10.0, 10.0, 10.0]) == 0.0
    assert weekly_cv([10.0]) is None
    assert weekly_cv([0.0, 0.0]) is None  # non-positive mean → undefined
    steady = weekly_cv([9.0, 10.0, 11.0, 10.0])
    bursty = weekly_cv([0.0, 20.0, 0.0, 20.0])
    assert bursty > steady


def test_boom_bust_history_widens_the_spread():
    steady = spread_ratios(
        [10.0] * 16, position="WR", proj_games=16, shrink_weight=0.1
    )
    bursty = spread_ratios(
        [0.0, 20.0] * 8, position="WR", proj_games=16, shrink_weight=0.1
    )
    assert bursty.rel_sigma > steady.rel_sigma
    assert bursty.ceiling_ratio > steady.ceiling_ratio
    assert bursty.floor_ratio < steady.floor_ratio


def test_thin_sample_is_wider_than_established_at_same_cv():
    weeks = [8.0, 12.0, 10.0, 9.0, 11.0, 10.0, 12.0, 8.0]
    established = spread_ratios(weeks, position="RB", proj_games=16, shrink_weight=0.1)
    thin = spread_ratios(weeks, position="RB", proj_games=16, shrink_weight=0.8)
    assert thin.rel_sigma > established.rel_sigma


def test_more_games_tightens_the_relative_spread():
    weeks = [5.0, 15.0] * 6
    short = spread_ratios(weeks, position="RB", proj_games=8, shrink_weight=0.2)
    full = spread_ratios(weeks, position="RB", proj_games=17, shrink_weight=0.2)
    assert full.rel_sigma < short.rel_sigma


def test_ratios_bracket_one_and_floor_is_clamped():
    est = spread_ratios([0.0, 40.0, 0.0, 40.0], position="TE", proj_games=1,
                        shrink_weight=1.0,
                        model=SpreadModel(max_rel_sigma=5.0, role_cv_shrink=1.0))
    assert est.floor_ratio == 0.0  # clamped, never negative
    assert est.ceiling_ratio > 1.0
    normal = spread_ratios([10.0] * 10, position="QB", proj_games=17, shrink_weight=0.0)
    assert 0.0 < normal.floor_ratio < 1.0 < normal.ceiling_ratio
    assert math.isclose(1.0 - normal.floor_ratio, normal.ceiling_ratio - 1.0, abs_tol=1e-6)


def test_few_weeks_fall_back_to_the_positional_prior():
    est = spread_ratios([30.0, 2.0], position="TE", proj_games=17, shrink_weight=0.5)
    # Two weeks is below MIN_WEEKS_FOR_OWN_CV → the TE prior, untouched.
    assert est.weekly_cv == pytest.approx(POSITION_CV_PRIOR["TE"], abs=1e-4)


def test_positional_priors_from_qualified_players_only():
    weekly = {"a": [10.0, 10.0] * 5, "b": [0.0, 20.0] * 5, "c": [0.0, 30.0]}
    priors = positional_cv_priors(weekly, {"a": "WR", "b": "WR", "c": "TE"}, min_weeks=8)
    assert priors["WR"] == pytest.approx((0.0 + 1.0) / 2)
    assert priors["TE"] == POSITION_CV_PRIOR["TE"]  # c too thin → fallback


def test_widen_for_disagreement_only_widens_and_is_symmetric():
    floor, ceiling = widen_for_disagreement(200.0, 170.0, 230.0, [200.0, 200.0])
    assert (floor, ceiling) == (170.0, 230.0)  # agreement → unchanged
    floor2, ceiling2 = widen_for_disagreement(200.0, 170.0, 230.0, [180.0, 220.0])
    assert floor2 < 170.0 and ceiling2 > 230.0
    assert math.isclose(200.0 - floor2, ceiling2 - 200.0, abs_tol=1e-6)
    assert widen_for_disagreement(200.0, None, None, [1.0, 2.0]) == (None, None)


# -- the nflverse source emits + caches the spread -----------------------------


def _weekly_rows():
    rows = []
    for season in (2025, 2024):
        for week in range(1, 18):
            rows.append({  # steady WR
                "player_id": "00-STEADY", "player_display_name": "Steady Wideout",
                "position": "WR", "recent_team": "KC", "season": season, "week": week,
                "season_type": "REG", "receptions": 5, "receiving_yards": 60,
                "receiving_tds": 0.0,
            })
            rows.append({  # boom-bust WR: same total, alternating weeks
                "player_id": "00-BOOM", "player_display_name": "Boom Wideout",
                "position": "WR", "recent_team": "KC", "season": season, "week": week,
                "season_type": "REG",
                "receptions": 10 if week % 2 else 0,
                "receiving_yards": 120 if week % 2 else 0,
                "receiving_tds": 0.0,
            })
    return rows


def _source(tmp_path):
    return NflverseProjectionSource(
        nflverse=NflverseSource(fetchers={"weekly": lambda years: _weekly_rows()}),
        history_seasons=2,
        cache_dir=tmp_path,
    )


def test_source_records_carry_floor_ceiling_and_bursty_is_wider(tmp_path):
    recs = {r.source_id: r for r in _source(tmp_path).warm_cache(2026)}
    for r in recs.values():
        assert r.floor is not None and r.ceiling is not None
        assert 0.0 <= r.floor <= r.points <= r.ceiling
    steady, boom = recs["00-STEADY"], recs["00-BOOM"]
    assert (boom.ceiling - boom.floor) / boom.points > (steady.ceiling - steady.floor) / steady.points


def test_cache_round_trips_floor_and_ceiling(tmp_path):
    src = _source(tmp_path)
    fresh = {r.source_id: r for r in src.warm_cache(2026)}
    cached = {r.source_id: r for r in src.project(season=2026)}
    for gid, r in fresh.items():
        assert cached[gid].floor == r.floor
        assert cached[gid].ceiling == r.ceiling


# -- the board ---------------------------------------------------------------


def _settings(risk_positions=("RB", 1)):
    return LeagueSettings(
        league_key="test.l.dist",
        max_teams=2,
        roster_positions=[
            RosterPosition(position="RB", count=1),
            RosterPosition(position="BN", count=1, is_starting_position=False),
        ],
    )


def _rec(gid, name, points, floor, ceiling, stats=None):
    return ProjectionRecord(
        source="test", source_id=gid, source_id_field="gsis_id",
        points=points, floor=floor, ceiling=ceiling, position="RB",
        team="KC", name=name, stats=stats or {},
    )


#: Two-team, one-RB league → baseline is the 3rd RB (100). Same median for
#: Steady/Boom (250 → VORP +150); Boom's range is twice as wide.
POOL = [
    _rec("S", "Steady", 250.0, 225.0, 275.0),
    _rec("B", "Boom", 250.0, 200.0, 300.0),
    _rec("D", "Depth", 100.0, 90.0, 110.0),
    _rec("N", "NoSpread", 90.0, None, None),
]


def _entry(board, name):
    return next(e for e in board.entries if e.name == name)


def test_board_brackets_points_and_vorp_with_the_spread():
    board = build_value_board(POOL, _settings())
    s = _entry(board, "Steady")
    assert (s.floor, s.points, s.ceiling) == (225.0, 250.0, 275.0)
    assert (s.floor_vorp, s.vorp, s.ceiling_vorp) == (125.0, 150.0, 175.0)
    n = _entry(board, "NoSpread")
    assert not n.has_distribution and n.floor_vorp is None


def test_spread_is_a_ratio_so_it_survives_league_rescoring():
    # A stat-line record: 100 receptions = 50 reference (half-PPR) points with
    # a 40/60 floor/ceiling; a full-PPR league scores it 100 → 80/120.
    rec = ProjectionRecord(
        source="t", source_id="P", source_id_field="gsis_id", points=50.0,
        floor=40.0, ceiling=60.0, position="RB", team="KC", name="Ppr",
        stats={"rec": 100.0},
    )
    settings = LeagueSettings(
        league_key="test.l.ppr", max_teams=1,
        stat_categories=[StatCategory(stat_id=11, value=1.0)],  # full PPR → 100
        roster_positions=[RosterPosition(position="RB", count=1)],
    )
    board = build_value_board([rec], settings)
    e = board.entries[0]
    assert e.points == 100.0
    assert (e.floor, e.ceiling) == (80.0, 120.0)  # 0.8× / 1.2× of league points


def test_risk_preference_zero_is_the_identity():
    base = build_value_board(POOL, _settings())
    same = build_value_board(POOL, _settings(), risk_preference=0.0)
    assert [e.canonical_id for e in base.entries] == [e.canonical_id for e in same.entries]
    assert all(e.draft_value is None for e in same.entries)


def test_positive_risk_lifts_the_high_ceiling_player_negative_the_safe_one():
    upside = build_value_board(POOL, _settings(), risk_preference=1.0)
    assert _entry(upside, "Boom").rank_value == 200.0  # ceiling VORP
    assert _entry(upside, "Steady").rank_value == 175.0
    assert upside.entries[0].name == "Boom"

    safe = build_value_board(POOL, _settings(), risk_preference=-1.0)
    assert _entry(safe, "Boom").rank_value == 100.0  # floor VORP
    assert _entry(safe, "Steady").rank_value == 125.0
    assert safe.entries[0].name == "Steady"
    # A no-spread entry never moves.
    assert _entry(safe, "NoSpread").draft_value is None


def test_risk_tilt_is_monotone_in_the_dial():
    values = [
        _entry(build_value_board(POOL, _settings(), risk_preference=r), "Boom").rank_value
        for r in (-1.0, -0.5, 0.0, 0.5, 1.0)
    ]
    assert values == sorted(values)
    assert values[2] == 150.0  # median at r=0


def test_risk_tilt_never_touches_raw_vorp_or_tiers():
    tilted = build_value_board(POOL, _settings(), risk_preference=0.7)
    plain = build_value_board(POOL, _settings())
    for name in ("Steady", "Boom", "Depth"):
        assert _entry(tilted, name).vorp == _entry(plain, name).vorp
        assert _entry(tilted, name).tier == _entry(plain, name).tier


# -- recommendation narration -------------------------------------------------


def test_recommendation_narrates_the_range_and_bet_type():
    board = build_value_board(POOL, _settings(), risk_preference=1.0)
    ranked = rank_available(board, compute_needs(roster_slots(_settings())))
    rec = build_recommendation(ranked, compute_needs(roster_slots(_settings())))
    assert rec.player.entry.name == "Boom"
    assert any("Floor 200 / median 250 / ceiling 300" in r for r in rec.reasons)
    d = rec.player.as_dict()
    assert (d["floor"], d["ceiling"], d["floor_vorp"], d["ceiling_vorp"]) == (200.0, 300.0, 100.0, 200.0)


def test_distribution_note_labels_wide_and_narrow():
    wide = _entry(build_value_board(
        [_rec("W", "Wide", 200.0, 120.0, 280.0)], _settings()), "Wide")
    narrow = _entry(build_value_board(
        [_rec("N", "Narrow", 200.0, 180.0, 220.0)], _settings()), "Narrow")
    assert "upside bet" in distribution_note(wide)
    assert "steady floor" in distribution_note(narrow)


# -- consensus + store carry the spread ---------------------------------------


def test_consensus_recentres_and_widens_the_spread(tmp_path):
    from tests.test_consensus import FakeModel, flat_curve, make_consensus

    model_rec = ProjectionRecord(
        source="fake", source_id="00-D", source_id_field="gsis_id",
        points=200.0, floor=160.0, ceiling=240.0, position="WR", team="KC",
        name="Blend Me", stats={"rec_yds": 2000.0, "games": 17.0},
    )
    fake = FakeModel([model_rec])
    # FakeModel.project copies only the point fields — carry the spread too.
    fake.project = lambda week=None, season=None: [model_rec]  # type: ignore[method-assign]
    src = make_consensus(
        tmp_path, model=fake, market={"00-D": 12.0},
        weights={"model": 0.5, "market": 0.5}, curve_fitter=flat_curve(300.0),
    )
    (rec,) = src._compute(2026)
    assert rec.points == pytest.approx(250.0)
    # Model ratios 0.8/1.2 re-centred on 250 → 200/300, then widened for the
    # 200-vs-300 disagreement: symmetric about the blend, strictly wider.
    assert rec.floor < 200.0 and rec.ceiling > 300.0
    assert math.isclose(250.0 - rec.floor, rec.ceiling - 250.0, abs_tol=0.02)
    # And it survives the consensus cache.
    src._write_cache(2026, [rec])
    (cached,) = src._load_cache(2026)
    assert (cached.floor, cached.ceiling) == (rec.floor, rec.ceiling)


def test_store_round_trips_projection_and_board_spread():
    from fantasy_coach.store import CoachStore

    with CoachStore(":memory:") as store:
        store.upsert_projections(POOL, season=2026)
        back = {r.source_id: r for r in store.projection_records(season=2026)}
        assert (back["S"].floor, back["S"].ceiling) == (225.0, 275.0)
        assert back["N"].floor is None
        settings = _settings()
        store.upsert_league_settings(settings)
        board = build_value_board(POOL, settings, risk_preference=0.4)
        store.replace_board(settings.league_key, board)
        rows = {r["name"]: r for r in store.get_board(settings.league_key)}
        assert rows["Boom"]["floor_vorp"] == 100.0
        assert rows["Boom"]["ceiling_vorp"] == 200.0
        assert store.board_meta(settings.league_key)["risk_preference"] == 0.4
