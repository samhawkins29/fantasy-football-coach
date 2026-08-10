"""Tests for schedule-aware valuation (step 5): playoff value, the blended
draft value, and the bye-stacking nudge.

Every number is hand-computable. The load-bearing invariants:

* playoff weeks come from the league's own settings (start week + one week
  per elimination round), Yahoo defaults as the labelled fallback;
* the weekly split is season total ÷ game weeks × opponent multiplier, so an
  easier playoff opponent → higher playoff value (direction test);
* playoff value sums ONLY the league's playoff weeks — early-season matchups
  cannot move it;
* the blend is monotonic in the emphasis weight, is exactly the season board
  at ``w=0`` (off-by-default safety), and reranks toward strong-playoff-
  schedule players as ``w`` rises;
* a neutral schedule (all multipliers 1.0) makes the annualized playoff VORP
  equal season VORP, so the blend is the identity at ANY weight — the dial
  only moves players whose playoff schedule actually differs;
* the bye-stacking penalty fires only past the free collision, scales down
  positive scores a few percent, and is narrated in the recommendation.
"""

from __future__ import annotations

import pytest

from fantasy_coach.clients.models import LeagueSettings, RosterPosition
from fantasy_coach.ingest.schedule import SeasonSchedule
from fantasy_coach.ingest.sources import ProjectionRecord
from fantasy_coach.value.board import build_value_board
from fantasy_coach.value.schedule import blend_value, playoff_weeks, weekly_points
from fantasy_coach.draft.recommend import (
    build_recommendation,
    compute_needs,
    rank_available,
    roster_slots,
)


def make_settings(
    roster: list[tuple[str, int]],
    *,
    max_teams: int = 3,
    playoff_start_week: int | None = 15,
    num_playoff_teams: int | None = 6,
) -> LeagueSettings:
    return LeagueSettings(
        league_key="test.l.step5",
        max_teams=max_teams,
        uses_playoff=True,
        playoff_start_week=playoff_start_week,
        num_playoff_teams=num_playoff_teams,
        roster_positions=[
            RosterPosition(
                position=pos, count=count,
                is_starting_position=pos not in ("BN", "IR"),
            )
            for pos, count in roster
        ],
    )


def proj(gsis: str, name: str, position: str, points: float, team: str) -> ProjectionRecord:
    # stats={} -> the board scores from `points` directly (conftest pattern).
    return ProjectionRecord(
        source="test", source_id=gsis, source_id_field="gsis_id",
        points=points, position=position, team=team, name=name, stats={},
    )


def team_season(
    playoff_opps: tuple[str, str, str] = ("N1", "N2", "N3"),
    *,
    bye: int = 9,
    other_opp: str = "NEU",
) -> dict[int, str]:
    """Weeks 1–17 minus the bye; weeks 15–17 against ``playoff_opps``."""
    games = {w: other_opp for w in range(1, 18) if w != bye}
    for i, w in enumerate((15, 16, 17)):
        if w in games:
            games[w] = playoff_opps[i]
    return games


#: Four one-position teams: AAA meets easy playoff defenses (1.2×), BBB tough
#: (0.8×), CCC/DDD neutral. Everyone byes week 9 → 16 game weeks through wk 17.
def make_schedule(rb_multipliers: dict[str, float] | None = None) -> SeasonSchedule:
    mults = {"EZ1": 1.2, "EZ2": 1.2, "EZ3": 1.2, "HD1": 0.8, "HD2": 0.8, "HD3": 0.8}
    if rb_multipliers is not None:
        mults = rb_multipliers
    return SeasonSchedule(
        season=2026,
        opponents={
            "AAA": team_season(("EZ1", "EZ2", "EZ3")),
            "BBB": team_season(("HD1", "HD2", "HD3")),
            "CCC": team_season(),
            "DDD": team_season(),
        },
        byes={"AAA": 9, "BBB": 9, "CCC": 9, "DDD": 9},
        multipliers={"RB": mults},
    )


#: 3-team, 1-RB league: dedicated demand 3 → baseline = 4th RB (100 pts).
#: Season VORP: Alpha/Beta +220, Carl +210, Dave 0 — all hand-checkable.
RB_POOL = [
    proj("A1", "Alpha Back", "RB", 320.0, "AAA"),
    proj("B1", "Beta Back", "RB", 320.0, "BBB"),
    proj("C1", "Carl Back", "RB", 310.0, "CCC"),
    proj("D1", "Dave Back", "RB", 100.0, "DDD"),
]
RB_SETTINGS = make_settings([("RB", 1), ("BN", 1)])


def rb_board(**kwargs):
    return build_value_board(RB_POOL, RB_SETTINGS, **kwargs)


def entry(board, name):
    return next(e for e in board.entries if e.name == name)


# -- playoff weeks from the league's own settings ------------------------------


@pytest.mark.parametrize(
    ("start", "teams", "expected"),
    [
        (15, 6, [15, 16, 17]),   # 6-team bracket = 3 elimination rounds
        (15, 4, [15, 16]),
        (16, 2, [16]),
        (17, 6, [17, 18]),       # capped at the NFL's week 18
        (None, None, [15, 16, 17]),  # Yahoo-default fallback
    ],
)
def test_playoff_weeks_from_settings(start, teams, expected):
    settings = make_settings([("RB", 1)], playoff_start_week=start, num_playoff_teams=teams)
    assert playoff_weeks(settings) == expected


# -- the weekly split ----------------------------------------------------------


def test_weekly_split_is_even_over_game_weeks_times_multiplier():
    schedule = make_schedule()
    weekly = weekly_points(320.0, "AAA", "RB", schedule, through_week=17)
    assert len(weekly) == 16              # 17 weeks minus the bye
    assert 9 not in weekly                # bye week has no entry
    assert weekly[1] == pytest.approx(20.0)          # neutral: 320/16
    assert weekly[15] == pytest.approx(24.0)         # 20 × 1.2 easy playoff opp
    assert weekly_points(320.0, "ZZZ", "RB", schedule, through_week=17) == {}


def test_easier_opponent_raises_weekly_value():
    schedule = make_schedule()
    easy = weekly_points(320.0, "AAA", "RB", schedule, through_week=17)
    tough = weekly_points(320.0, "BBB", "RB", schedule, through_week=17)
    assert easy[15] > tough[15]           # 24.0 vs 16.0 — direction holds


# -- playoff value on the board ------------------------------------------------


def test_board_playoff_value_hand_computed():
    board = rb_board(schedule=make_schedule(), playoff_weight=0.5)
    alpha = entry(board, "Alpha Back")
    # 320/16 = 20/wk; playoff = 20×1.2×3 = 72; baseline share = 100×3/16 = 18.75.
    assert alpha.playoff_points == pytest.approx(72.0)
    assert alpha.playoff_vorp == pytest.approx(53.25)
    # Annualized: 53.25 × 16/3 = 284; blend at w=0.5 with VORP 220 → 252.
    assert alpha.draft_value == pytest.approx(252.0)
    assert "soft playoff schedule" in alpha.schedule_note


def test_playoff_value_computed_only_over_playoff_weeks():
    board = rb_board(schedule=make_schedule(), playoff_weight=0.5)
    # Same schedule but early-season opponent NEU suddenly reads 1.25× easy:
    # playoff weeks don't include NEU, so playoff value must not move.
    juiced = make_schedule(
        {"EZ1": 1.2, "EZ2": 1.2, "EZ3": 1.2, "HD1": 0.8, "HD2": 0.8, "HD3": 0.8, "NEU": 1.25}
    )
    board2 = rb_board(schedule=juiced, playoff_weight=0.5)
    assert entry(board2, "Alpha Back").playoff_points == entry(board, "Alpha Back").playoff_points
    assert entry(board2, "Beta Back").playoff_vorp == entry(board, "Beta Back").playoff_vorp


def test_weight_zero_is_exactly_the_season_board():
    plain = rb_board()
    with_schedule = rb_board(schedule=make_schedule(), playoff_weight=0.0)
    assert [e.name for e in with_schedule.entries] == [e.name for e in plain.entries]
    for e in with_schedule.entries:
        assert e.draft_value == e.vorp          # blend at w=0 is the identity
        assert e.playoff_vorp is not None       # …but the info still surfaces


def test_neutral_schedule_blend_is_identity_at_any_weight():
    # All multipliers 1.0: even split makes annualized playoff VORP == season
    # VORP, so the dial only moves players whose schedule actually differs.
    neutral = make_schedule({})
    board = rb_board(schedule=neutral, playoff_weight=0.7)
    for e in board.entries:
        assert e.draft_value == pytest.approx(e.vorp, abs=0.05)


def test_blended_value_monotonic_in_emphasis_weight():
    values = [
        entry(rb_board(schedule=make_schedule(), playoff_weight=w), "Alpha Back").draft_value
        for w in (0.0, 0.25, 0.5, 1.0)
    ]
    assert values == sorted(values)          # strong playoff schedule: rises
    assert values[0] == pytest.approx(220.0) and values[-1] == pytest.approx(284.0)
    tough = [
        entry(rb_board(schedule=make_schedule(), playoff_weight=w), "Beta Back").draft_value
        for w in (0.0, 0.25, 0.5, 1.0)
    ]
    assert tough == sorted(tough, reverse=True)  # tough playoff schedule: falls


def test_raising_emphasis_reranks_toward_playoff_schedule():
    # Alpha and Beta tie on season VORP (alphabetical order breaks the tie),
    # neutral Carl sits just below them at +210.
    flat = rb_board(schedule=make_schedule(), playoff_weight=0.0)
    assert [e.name for e in flat.top(3)] == ["Alpha Back", "Beta Back", "Carl Back"]
    tilted = rb_board(schedule=make_schedule(), playoff_weight=0.35)
    # w=0.35: Beta 220 → 0.65×220 + 0.35×156 = 197.6 — now below neutral Carl.
    assert [e.name for e in tilted.top(3)] == ["Alpha Back", "Carl Back", "Beta Back"]
    assert entry(tilted, "Beta Back").overall_rank > entry(flat, "Beta Back").overall_rank
    assert entry(tilted, "Beta Back").draft_value == pytest.approx(197.6, abs=0.1)
    # VORP itself and the tiers never move with the dial (season truth stays).
    assert entry(tilted, "Beta Back").vorp == entry(flat, "Beta Back").vorp
    assert entry(tilted, "Beta Back").tier == entry(flat, "Beta Back").tier


def test_bye_week_filled_from_schedule():
    board = rb_board(schedule=make_schedule(), playoff_weight=0.0)
    assert all(e.bye_week == 9 for e in board.entries)


# -- the bye-stacking nudge (recommendation layer) -----------------------------


def bye_schedule() -> SeasonSchedule:
    """Neutral multipliers; AAA/CCC/DDD bye week 9, BBB bye week 7."""
    return SeasonSchedule(
        season=2026,
        opponents={
            "AAA": team_season(), "CCC": team_season(), "DDD": team_season(),
            "BBB": team_season(bye=7),
        },
        byes={"AAA": 9, "CCC": 9, "DDD": 9, "BBB": 7},
        multipliers={},
    )


def ranked_with_byes(starter_bye_counts):
    board = rb_board(schedule=bye_schedule(), playoff_weight=0.0)
    needs = compute_needs(roster_slots(RB_SETTINGS))   # RB starter open
    return rank_available(board, needs, starter_bye_counts=starter_bye_counts)


def test_one_shared_bye_starter_is_free():
    ranked = ranked_with_byes({9: 1})
    assert [rp.entry.name for rp in ranked[:2]] == ["Alpha Back", "Beta Back"]
    assert ranked[0].score == ranked[1].score == 220.0   # no penalty either way


def test_bye_stacking_penalty_fires_past_the_free_collision():
    # Two of my starters already bye in week 9: Alpha (bye 9) is nudged below
    # equal-value Beta (bye 7). 220 × (1 − 0.04) = 211.2.
    ranked = ranked_with_byes({9: 2})
    assert [rp.entry.name for rp in ranked[:2]] == ["Beta Back", "Alpha Back"]
    alpha = next(rp for rp in ranked if rp.entry.name == "Alpha Back")
    assert alpha.score == pytest.approx(211.2)
    assert alpha.bye_overlap == 2
    assert next(rp for rp in ranked if rp.entry.name == "Beta Back").score == 220.0


def test_bye_penalty_is_capped_and_never_inflates_negatives():
    ranked = ranked_with_byes({9: 99})
    alpha = next(rp for rp in ranked if rp.entry.name == "Alpha Back")
    assert alpha.score == pytest.approx(220.0 * (1 - 0.12))   # BYE_PENALTY_MAX
    dave = next(rp for rp in ranked if rp.entry.name == "Dave Back")
    assert dave.score == 0.0                                   # zero stays zero


def test_recommendation_narrates_playoff_schedule_and_bye_stack():
    board = rb_board(schedule=make_schedule(), playoff_weight=0.5)
    needs = compute_needs(roster_slots(RB_SETTINGS))
    ranked = rank_available(board, needs, starter_bye_counts={9: 2})
    rec = build_recommendation(ranked, needs, current_pick=1)
    assert rec is not None and rec.player.entry.name == "Alpha Back"
    text = " ".join(rec.reasons)
    assert "draft value" in text and "playoff-blended" in text
    assert "Soft playoff schedule" in text
    assert "Bye 9 already shared by 2" in text


def test_recommendation_reasons_unchanged_without_schedule():
    board = rb_board()
    needs = compute_needs(roster_slots(RB_SETTINGS))
    rec = build_recommendation(rank_available(board, needs), needs, current_pick=1)
    text = " ".join(rec.reasons)
    assert "VORP +220.0" in text
    assert "playoff" not in text.lower() and "bye" not in text.lower()


# -- integration: draft loop + store + migration -------------------------------


class StaticPickSource:
    """A pick source that always reports an empty room."""

    def fetch(self):
        return []


def test_draft_loop_carries_playoff_fields_into_snapshot(draft_store, draft_settings):
    from tests.conftest import DRAFT_LEAGUE_KEY
    from fantasy_coach.draft.loop import DraftLoop

    # Every pool player is on KC (conftest); neutral multipliers → the blend is
    # the identity, so the recommendation must match the season board exactly.
    schedule = SeasonSchedule(
        season=2026, opponents={"KC": team_season()}, byes={"KC": 9}, multipliers={}
    )
    loop = DraftLoop(
        draft_store,
        draft_settings,
        StaticPickSource(),
        my_team_key=f"{DRAFT_LEAGUE_KEY}.t.1",
        record_to_store=False,
        schedule=schedule,
        playoff_weight=0.3,
        time_func=lambda: 1000.0,
        sleep_func=lambda s: None,
    )
    snap = loop.poll_once()
    assert snap["playoff"] == {"weight": 0.3, "weeks": [15, 16, 17], "schedule_loaded": True}
    rec = snap["recommendation"]
    assert rec["playoff_vorp"] is not None
    assert rec["draft_value"] == pytest.approx(rec["vorp"], abs=0.05)
    assert all(p["bye"] == 9 for p in snap["available"])


def test_store_round_trips_playoff_board_columns(draft_settings):
    import json

    from fantasy_coach.store import CoachStore

    board = rb_board(schedule=make_schedule(), playoff_weight=0.5)
    with CoachStore(":memory:") as store:
        store.upsert_league_settings(RB_SETTINGS)
        store.replace_board(RB_SETTINGS.league_key, board)
        rows = {r["name"]: r for r in store.get_board(RB_SETTINGS.league_key)}
        assert rows["Alpha Back"]["draft_value"] == pytest.approx(252.0)
        assert rows["Alpha Back"]["playoff_vorp"] == pytest.approx(53.25)
        assert "soft playoff schedule" in rows["Alpha Back"]["schedule_note"]
        meta = store.board_meta(RB_SETTINGS.league_key)
        assert meta["playoff_weight"] == 0.5
        assert json.loads(meta["playoff_weeks"]) == [15, 16, 17]


def test_migration_rerun_tolerates_existing_columns():
    # A crash between a v2 ALTER and its version bump re-runs the batch; the
    # duplicate-column tolerance must absorb that instead of failing forever.
    import sqlite3

    from fantasy_coach.store.schema import SCHEMA_VERSION, apply_migrations

    conn = sqlite3.connect(":memory:")
    assert apply_migrations(conn) == SCHEMA_VERSION
    conn.execute("PRAGMA user_version = 1")  # pretend v2 never recorded
    assert apply_migrations(conn) == SCHEMA_VERSION  # ALTERs re-run harmlessly
