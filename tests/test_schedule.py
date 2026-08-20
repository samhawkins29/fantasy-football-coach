"""Tests for the schedule + opponent-difficulty source (step 5 data layer).

All offline via injected fetchers (the ``NflverseSource(fetchers=...)`` seam).
Load-bearing invariants:

* the opponent map is symmetric and byes fall out of the missing week;
* multiplier direction: a defense that allowed MORE fantasy points to a
  position reads > 1.0 (easier), fewer < 1.0 (harder), clamped, mean ≈ 1;
* rows without an opponent column degrade to all-neutral, never crash;
* the JSON cache round-trips exactly and serves with zero fetches.
"""

from __future__ import annotations

import pytest

from fantasy_coach.ingest.schedule import SCHEDULE_NOTE, ScheduleSource, SeasonSchedule
from fantasy_coach.ingest.sources import NflverseSource


def sched_row(week: int, home: str, away: str, *, game_type: str = "REG", season: int = 2026) -> dict:
    return {
        "season": season,
        "week": week,
        "home_team": home,
        "away_team": away,
        "game_type": game_type,
    }


def weekly_row(
    position: str,
    opponent: str | None,
    *,
    season: int = 2025,
    week: int = 1,
    team: str = "KC",
    **stats: float,
) -> dict:
    row: dict = {
        "season": season,
        "week": week,
        "season_type": "REG",
        "position": position,
        "team": team,
        "player_id": "00-0000001",
    }
    if opponent is not None:
        row["opponent_team"] = opponent
    row.update(stats)
    return row


def make_source(schedule_rows, weekly_rows, cache_dir, **kwargs) -> ScheduleSource:
    return ScheduleSource(
        nflverse=NflverseSource(
            fetchers={
                "schedules": lambda years: list(schedule_rows),
                "weekly": lambda years: list(weekly_rows),
            }
        ),
        cache_dir=cache_dir,
        **kwargs,
    )


# -- opponent map + byes -------------------------------------------------------


def test_opponent_map_is_symmetric_and_byes_derive_from_missing_week(tmp_path):
    rows = [
        sched_row(1, "KC", "DEN"), sched_row(1, "LV", "LAC"),
        sched_row(2, "KC", "LV"),                      # DEN + LAC on bye week 2
        sched_row(3, "DEN", "LV"), sched_row(3, "LAC", "KC"),
    ]
    schedule = make_source(rows, [], tmp_path).warm_cache(2026)

    assert schedule.opponent("KC", 1) == "DEN"
    assert schedule.opponent("DEN", 1) == "KC"        # symmetric
    assert schedule.opponent("DEN", 2) is None        # bye
    assert schedule.bye_week("DEN") == 2
    assert schedule.bye_week("LAC") == 2
    assert schedule.bye_week("KC") is None            # played every week
    assert schedule.game_weeks("DEN", through=3) == [1, 3]
    assert schedule.game_weeks("KC", through=2) == [1, 2]


def test_non_regular_season_games_are_ignored(tmp_path):
    rows = [sched_row(1, "KC", "DEN"), sched_row(19, "KC", "BUF", game_type="WC")]
    schedule = make_source(rows, [], tmp_path).warm_cache(2026)
    assert schedule.opponent("KC", 19) is None
    assert schedule.game_weeks("KC", through=22) == [1]


# -- opponent-difficulty multipliers ------------------------------------------


def test_multiplier_direction_more_points_allowed_reads_easier(tmp_path):
    # DEN allowed 110 rush yds (11 pts), LV allowed 90 (9 pts) in their one
    # game -> mean 10 -> raw ratios 1.1 / 0.9, shrunk toward neutral by the
    # default k=0.35 (one season of defense data is noise, not truth):
    # DEN 1 + 0.35x0.1 = 1.035 (easier), LV 0.965 (harder). Hand-computable.
    weekly = [
        weekly_row("RB", "DEN", week=1, rushing_yards=110.0),
        weekly_row("RB", "LV", week=1, rushing_yards=90.0),
    ]
    schedule = make_source([], weekly, tmp_path).warm_cache(2026)
    assert schedule.multiplier("RB", "DEN") == pytest.approx(1.035)
    assert schedule.multiplier("RB", "LV") == pytest.approx(0.965)
    assert schedule.multiplier("RB", "SEA") == 1.0    # unseen defense: neutral
    assert schedule.multiplier("WR", "DEN") == 1.0    # unseen position: neutral
    assert schedule.sos_seasons == [2025]


def test_multiplier_is_per_game_and_shrunk(tmp_path):
    # DEN bleeds 300 rush yds across 2 games (15/game), LV 50 in 1 game (5).
    # Mean 10 -> raw 1.5 / 0.5, shrunk (k=0.35) to 1.175 / 0.825 — inside the
    # clamp, which stays as a backstop for absurd inputs.
    weekly = [
        weekly_row("RB", "DEN", week=1, rushing_yards=180.0),
        weekly_row("RB", "DEN", week=2, rushing_yards=120.0),
        weekly_row("RB", "LV", week=1, rushing_yards=50.0),
    ]
    schedule = make_source([], weekly, tmp_path).warm_cache(2026)
    assert schedule.multiplier("RB", "DEN") == pytest.approx(1.175)
    assert schedule.multiplier("RB", "LV") == pytest.approx(0.825)


def test_multiplier_clamp_backstops_extreme_shrunk_ratios(tmp_path):
    # A grotesque outlier (raw ratio ~1.9+) still cannot exceed the clamp
    # even after shrinking: shrink first, clamp second.
    from fantasy_coach.ingest.schedule import ScheduleSource

    weekly = [
        weekly_row("RB", "DEN", week=1, rushing_yards=1000.0),
        weekly_row("RB", "LV", week=1, rushing_yards=10.0),
    ]
    source = make_source([], weekly, tmp_path)
    source.shrink = 1.0  # no shrink: raw ratios ~1.98 / 0.02 hit the clamp
    schedule = source.warm_cache(2026)
    assert schedule.multiplier("RB", "DEN") == 1.25
    assert schedule.multiplier("RB", "LV") == 0.75


def test_rows_without_opponent_column_degrade_to_all_neutral(tmp_path):
    weekly = [weekly_row("RB", None, rushing_yards=150.0)]
    schedule = make_source([sched_row(1, "KC", "DEN")], weekly, tmp_path).warm_cache(2026)
    assert schedule.multipliers == {}
    assert schedule.multiplier("RB", "DEN") == 1.0
    assert schedule.sos_seasons == []


# -- cache (framework §7 zero-network draft day) -------------------------------


def test_cache_round_trips_and_serves_with_zero_fetches(tmp_path):
    rows = [sched_row(1, "KC", "DEN"), sched_row(2, "KC", "LV"), sched_row(2, "DEN", "LAC")]
    weekly = [
        weekly_row("WR", "DEN", receiving_yards=120.0, receptions=8.0),
        weekly_row("WR", "LV", receiving_yards=80.0, receptions=6.0),
    ]
    warmed = make_source(rows, weekly, tmp_path).warm_cache(2026)

    def explode(*args, **kwargs):  # any fetch after warm is a test failure
        raise AssertionError("draft day must not fetch")

    offline = ScheduleSource(
        nflverse=NflverseSource(fetchers={"schedules": explode, "weekly": explode}),
        cache_dir=tmp_path,
    )
    loaded = offline.load(2026)
    assert loaded.opponents == warmed.opponents          # int week keys restored
    assert loaded.byes == warmed.byes
    assert loaded.multipliers == warmed.multipliers
    assert loaded.note == SCHEDULE_NOTE


def test_load_without_cache_and_dead_fetch_raises_runtime_error(tmp_path):
    def explode(*args, **kwargs):
        raise OSError("no network")

    source = ScheduleSource(
        nflverse=NflverseSource(fetchers={"schedules": explode, "weekly": explode}),
        cache_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="warm_cache"):
        source.load(2026)


def test_corrupt_cache_is_treated_as_absent(tmp_path):
    source = make_source([sched_row(1, "KC", "DEN")], [], tmp_path)
    source.warm_cache(2026)
    source._cache_path(2026).write_text("{not json", encoding="utf-8")
    schedule = source.load(2026)  # recomputes live from the fetchers
    assert schedule.opponent("KC", 1) == "DEN"


def test_season_schedule_normalizes_team_and_position_lookups():
    schedule = SeasonSchedule(
        season=2026,
        opponents={"LV": {1: "KC"}},
        byes={"LV": 8},
        multipliers={"RB": {"KC": 1.2}},
    )
    assert schedule.opponent("OAK", 1) == "KC"       # legacy code normalizes
    assert schedule.bye_week("OAK") == 8
    assert schedule.multiplier("rb", "KC") == 1.2
