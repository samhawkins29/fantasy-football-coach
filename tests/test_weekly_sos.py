"""Per-week strength of schedule (upgrade 3).

Invariants:

* ``SeasonSchedule.week_multipliers`` is the position-specific per-week
  matchup profile — an RB's Week-15 entry is *that* defense's RB multiplier,
  byes have no entry, unknown teams give ``{}``;
* ``weighted_sos`` counts playoff weeks heavier (2×) than regular weeks;
* the board's ``sos_vorp`` values every week through its own opponent (a hard
  early-season slate lowers it, an easy one raises it) and ``sos_weight=0``
  is the identity on the pre-upgrade board — with the dial up, the season
  component moves toward ``sos_vorp`` monotonically;
* SOS and the playoff emphasis compose: playoff weeks are still weighted
  heavier on top of the per-week season adjustment;
* the schedule note names an extreme playoff-week matchup;
* the recommendation dict exposes the per-week playoff matchups.
"""

from __future__ import annotations

import pytest

from fantasy_coach.ingest.schedule import SeasonSchedule
from fantasy_coach.value.board import build_value_board
from fantasy_coach.value.schedule import (
    PLAYOFF_WEEK_WEIGHT,
    extreme_playoff_week,
    schedule_note,
    sos_blend,
    weighted_sos,
)
from fantasy_coach.draft.recommend import compute_needs, rank_available, roster_slots
from tests.test_playoff_value import (
    RB_SETTINGS,
    make_schedule,
    make_settings,
    proj,
    team_season,
)


def _entry(board, name):
    return next(e for e in board.entries if e.name == name)


# -- the per-week profile -----------------------------------------------------


def test_week_multipliers_are_position_specific_and_skip_the_bye():
    sched = SeasonSchedule(
        season=2026,
        opponents={"AAA": {1: "X", 2: "Y", 4: "X"}},
        byes={"AAA": 3},
        multipliers={"RB": {"X": 1.2, "Y": 0.8}, "WR": {"X": 0.9}},
    )
    assert sched.week_multipliers("AAA", "RB") == {1: 1.2, 2: 0.8, 4: 1.2}
    assert sched.week_multipliers("AAA", "WR") == {1: 0.9, 2: 1.0, 4: 0.9}
    assert sched.week_multipliers("AAA", "RB", through=2) == {1: 1.2, 2: 0.8}
    assert sched.week_multipliers("ZZZ", "RB") == {}


def test_weighted_sos_counts_playoff_weeks_heavier():
    mults = {14: 1.0, 15: 1.2, 16: 1.2, 17: 1.2}
    plain = sum(mults.values()) / 4
    weighted = weighted_sos(mults, [15, 16, 17])
    assert weighted > plain
    assert weighted == pytest.approx((1.0 + PLAYOFF_WEEK_WEIGHT * 3.6) / (1 + 3 * PLAYOFF_WEEK_WEIGHT))
    assert weighted_sos({}, [15]) is None
    assert weighted_sos({1: 0.9, 2: 1.1}, []) == pytest.approx(1.0)


def test_sos_blend_is_identity_at_zero_and_full_at_one():
    assert sos_blend(100.0, 80.0, weight=0.0) == 100.0
    assert sos_blend(100.0, 80.0, weight=1.0) == 80.0
    assert sos_blend(100.0, 80.0, weight=0.25) == pytest.approx(95.0)


# -- the board ---------------------------------------------------------------


def _early_hard_schedule() -> SeasonSchedule:
    """AAA: neutral playoff weeks but a brutal weeks 1–14; BBB: all neutral."""
    return SeasonSchedule(
        season=2026,
        opponents={
            "AAA": team_season(("NEU", "NEU", "NEU"), other_opp="HARD"),
            "BBB": team_season(),
            "CCC": team_season(),
            "DDD": team_season(),
        },
        byes={"AAA": 9, "BBB": 9, "CCC": 9, "DDD": 9},
        multipliers={"RB": {"HARD": 0.8}},
    )


def test_sos_vorp_sees_every_week_where_playoff_vorp_does_not():
    board = build_value_board(
        [
            proj("A1", "Alpha Back", "RB", 320.0, "AAA"),
            proj("B1", "Beta Back", "RB", 320.0, "BBB"),
            proj("C1", "Carl Back", "RB", 310.0, "CCC"),
            proj("D1", "Dave Back", "RB", 100.0, "DDD"),
        ],
        RB_SETTINGS,
        schedule=_early_hard_schedule(),
    )
    a, b = _entry(board, "Alpha Back"), _entry(board, "Beta Back")
    assert a.playoff_vorp == b.playoff_vorp  # neutral playoff weeks both
    assert a.sos_vorp < b.sos_vorp  # but Alpha's early slate is brutal
    assert b.sos_vorp == pytest.approx(b.vorp)  # all-neutral → identity
    assert len(a.week_multipliers) == 16 and a.week_multipliers[15] == 1.0
    assert a.week_multipliers[1] == 0.8
    assert a.sos_score < 1.0 and b.sos_score == pytest.approx(1.0)


def test_sos_weight_zero_is_the_identity_and_the_dial_is_monotone():
    pool = [
        proj("A1", "Alpha Back", "RB", 320.0, "AAA"),
        proj("B1", "Beta Back", "RB", 320.0, "BBB"),
        proj("C1", "Carl Back", "RB", 310.0, "CCC"),
        proj("D1", "Dave Back", "RB", 100.0, "DDD"),
    ]
    sched = _early_hard_schedule()
    off = build_value_board(pool, RB_SETTINGS, schedule=sched, sos_weight=0.0)
    plain = build_value_board(pool, RB_SETTINGS, schedule=sched)
    assert [e.rank_value for e in off.entries] == [e.rank_value for e in plain.entries]
    assert _entry(off, "Alpha Back").rank_value == _entry(off, "Alpha Back").vorp
    values = [
        _entry(build_value_board(pool, RB_SETTINGS, schedule=sched, sos_weight=s),
               "Alpha Back").rank_value
        for s in (0.0, 0.25, 0.5, 1.0)
    ]
    assert values == sorted(values, reverse=True)  # hard slate: falls as s rises
    full = build_value_board(pool, RB_SETTINGS, schedule=sched, sos_weight=1.0)
    assert _entry(full, "Alpha Back").rank_value == pytest.approx(
        _entry(full, "Alpha Back").sos_vorp, abs=0.02
    )
    assert full.sos_weight == 1.0 and plain.sos_weight == 0.0


def test_sos_and_playoff_emphasis_compose():
    # AAA: easy playoff weeks (1.2×) but hard everything else; with SOS on and
    # the playoff dial on, both effects show: sos_vorp < vorp, playoff_vorp >
    # season share — and draft value sits between the two extremes.
    sched = SeasonSchedule(
        season=2026,
        opponents={
            "AAA": team_season(("EZ", "EZ", "EZ"), other_opp="HARD"),
            "BBB": team_season(), "CCC": team_season(), "DDD": team_season(),
        },
        byes={"AAA": 9, "BBB": 9, "CCC": 9, "DDD": 9},
        multipliers={"RB": {"HARD": 0.8, "EZ": 1.2}},
    )
    pool = [
        proj("A1", "Alpha Back", "RB", 320.0, "AAA"),
        proj("B1", "Beta Back", "RB", 320.0, "BBB"),
        proj("C1", "Carl Back", "RB", 310.0, "CCC"),
        proj("D1", "Dave Back", "RB", 100.0, "DDD"),
    ]
    sos_only = _entry(build_value_board(pool, RB_SETTINGS, schedule=sched, sos_weight=1.0), "Alpha Back")
    both = _entry(build_value_board(pool, RB_SETTINGS, schedule=sched, sos_weight=1.0, playoff_weight=0.5), "Alpha Back")
    assert sos_only.sos_vorp < sos_only.vorp
    assert both.playoff_vorp * (16 / 3) > both.vorp  # easy playoff weeks annualize high
    assert sos_only.rank_value < both.rank_value  # playoff dial lifts it back up
    assert both.sos_score > sos_only.sos_score - 1e-9  # same profile, same score


def test_schedule_note_names_an_extreme_playoff_week():
    sched = make_schedule({"EZ1": 1.0, "EZ2": 1.0, "EZ3": 1.3, "HD1": 0.8, "HD2": 0.8, "HD3": 0.8})
    # AAA: avg 1.1 → soft, with wk17 vs EZ3 called out.
    note = schedule_note("RB", "AAA", sched, [15, 16, 17])
    assert note.startswith("soft playoff schedule")
    assert "wk17 vs EZ3 1.30×" in note
    # A team with a neutral average but one brutal week gets a week-only note.
    sched2 = make_schedule({"EZ1": 1.15, "EZ2": 1.15, "EZ3": 0.7})
    note2 = schedule_note("RB", "AAA", sched2, [15, 16, 17])
    assert note2 == "tough wk17 matchup vs EZ3 (0.70× vs RB)"
    assert extreme_playoff_week("RB", "CCC", sched, [15, 16, 17]) is None


def test_ranked_dict_exposes_playoff_matchups_and_sos_fields():
    sched = make_schedule()
    board = build_value_board(
        [proj("A1", "Alpha Back", "RB", 320.0, "AAA"), proj("D1", "Dave Back", "RB", 100.0, "DDD")],
        make_settings([("RB", 1)], max_teams=1), schedule=sched, sos_weight=0.5,
    )
    ranked = rank_available(board, compute_needs(roster_slots(RB_SETTINGS)))
    d = ranked[0].as_dict()
    assert d["name"] == "Alpha Back"
    assert d["playoff_matchups"] == [
        {"week": 15, "mult": 1.2}, {"week": 16, "mult": 1.2}, {"week": 17, "mult": 1.2}
    ]
    assert d["sos_vorp"] is not None and d["sos_score"] > 1.0
