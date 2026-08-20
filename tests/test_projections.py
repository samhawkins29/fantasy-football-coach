"""Tests for the free nflverse-based projection model (M3→M4 seam).

All offline: the nflverse weekly pull is injected as plain lists of dicts
(no pandas, no network) — the standard M3 fixture pattern. Model knobs are
pinned to hand-computable values so every expected number below is derivable
on paper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fantasy_coach.config import Config
from fantasy_coach.ingest.projections import (
    PROJECTED_STAT_KEYS,
    REFERENCE_SCORING,
    NflverseProjectionSource,
    default_season,
    make_projection_source,
    score_stats,
)
from fantasy_coach.ingest.sources import (
    FantasyProsSource,
    NflverseSource,
    ProjectionRecord,
    ProjectionSource,
)


def week_row(
    gsis_id: str,
    name: str,
    position: str,
    team: str,
    season: int,
    week: int,
    **stats: float,
) -> dict:
    """One nflverse ``import_weekly_data``-shaped row (only the columns we read)."""
    row = {
        "player_id": gsis_id,
        "player_display_name": name,
        "position": position,
        "recent_team": team,
        "season": season,
        "week": week,
        "season_type": "REG",
    }
    row.update(stats)
    return row


def make_source(rows: list[dict], tmp_path: Path, **knobs) -> NflverseProjectionSource:
    """A projection source over injected weekly rows, cache in pytest's tmp dir."""
    return NflverseProjectionSource(
        nflverse=NflverseSource(fetchers={"weekly": lambda years: rows}),
        cache_dir=tmp_path / "cache",
        **knobs,
    )


# -- pure pieces -------------------------------------------------------------


def test_score_stats_reference_scoring():
    # Half-PPR reference: 100 rec yds + 1 rec TD + 4 rec = 10 + 6 + 2 = 18.
    assert score_stats({"rec_yds": 100, "rec_td": 1, "rec": 4}) == pytest.approx(18.0)
    # QB line: 250 pass yds (10) + 2 pass TD (8) + 1 INT (-1) = 17.
    assert score_stats({"pass_yds": 250, "pass_td": 2, "pass_int": 1}) == pytest.approx(17.0)


def test_score_stats_ignores_unknown_keys():
    assert score_stats({"games": 17, "mystery": 99}) == 0.0


def test_default_season_calendar_split():
    from datetime import date

    assert default_season(date(2026, 8, 10)) == 2026  # draft season
    assert default_season(date(2026, 1, 15)) == 2025  # still last season's playoffs


def test_projection_record_stats_default_empty():
    rec = ProjectionRecord(source="x", source_id="1", source_id_field="gsis_id", points=1.0)
    assert rec.stats == {}


# -- the model, hand-computable ----------------------------------------------


def test_single_season_rates_times_projected_games(tmp_path):
    # One RB, one season, 2 games, 170 rush yds + 2 TD. With shrinkage off and
    # games_regression=1.0 (project a full 17), season line = per-game * 17:
    # rush_yds: 85 * 17 = 1445 ; rush_td: 1 * 17 = 17.
    rows = [
        week_row("00-1", "Test Back", "RB", "SF", 2025, 1, rushing_yards=100.0, rushing_tds=1.0),
        week_row("00-1", "Test Back", "RB", "SF", 2025, 2, rushing_yards=70.0, rushing_tds=1.0),
    ]
    src = make_source(rows, tmp_path, history_seasons=1, season_weights=(1.0,), shrink_games=0.0, games_regression=1.0)
    (rec,) = src.project(season=2026)
    assert rec.source == "nflverse_model"
    assert rec.source_id == "00-1"
    assert rec.source_id_field == "gsis_id"
    assert rec.position == "RB"
    assert rec.team == "SF"
    assert rec.stats["rush_yds"] == pytest.approx(1445.0)
    assert rec.stats["rush_td"] == pytest.approx(17.0)
    assert rec.stats["games"] == pytest.approx(17.0)
    # points under reference scoring: 1445*0.1 + 17*6 = 144.5 + 102 = 246.5
    assert rec.points == pytest.approx(246.5)


def test_recency_weights_favor_recent_season(tmp_path):
    # Same player, 1 game each season: 100 yds in 2025, 50 in 2024. Weights
    # (0.75, 0.25) -> rate = (0.75*100 + 0.25*50) / (0.75*1 + 0.25*1) = 87.5.
    rows = [
        week_row("00-1", "A", "WR", "KC", 2025, 1, receiving_yards=100.0),
        week_row("00-1", "A", "WR", "KC", 2024, 1, receiving_yards=50.0),
    ]
    src = make_source(
        rows, tmp_path, history_seasons=2, season_weights=(0.75, 0.25), shrink_games=0.0, games_regression=1.0
    )
    (rec,) = src.project(season=2026)
    assert rec.stats["rec_yds"] == pytest.approx(87.5 * 17)


def test_low_sample_player_regresses_toward_positional_prior(tmp_path):
    # Qualified WR baseline: two 10-game players at 100 and 60 rec yds/game ->
    # prior mean 80, damped by 0.5 -> 40. A 1-game player who exploded for 150
    # is shrunk with K=4: (1*150 + 4*40) / (1+4) = 62 yds/game — pulled hard
    # toward the prior, not projected as a 150/game monster.
    rows = []
    for wk in range(1, 11):
        rows.append(week_row("00-A", "Stud", "WR", "DAL", 2025, wk, receiving_yards=100.0))
        rows.append(week_row("00-B", "Solid", "WR", "MIA", 2025, wk, receiving_yards=60.0))
    rows.append(week_row("00-C", "One Game Wonder", "WR", "NYJ", 2025, 1, receiving_yards=150.0))
    src = make_source(
        rows,
        tmp_path,
        history_seasons=1,
        season_weights=(1.0,),
        shrink_games=4.0,
        prior_damping=0.5,
        min_prior_games=8.0,
        games_regression=1.0,
    )
    by_id = {r.source_id: r for r in src.project(season=2026)}
    assert by_id["00-C"].stats["rec_yds"] == pytest.approx(62.0 * 17)
    # The stud is barely moved: (10*100 + 4*40)/14 = 82.86/game.
    assert by_id["00-A"].stats["rec_yds"] == pytest.approx((10 * 100 + 4 * 40) / 14 * 17, rel=1e-4)


def test_games_projection_regresses_injury_history_to_full_season(tmp_path):
    # 8 games played, games_regression=0.5 -> proj_games = 0.5*17 + 0.5*8 = 12.5.
    rows = [
        week_row("00-1", "Fragile", "RB", "GB", 2025, wk, rushing_yards=100.0) for wk in range(1, 9)
    ]
    src = make_source(rows, tmp_path, history_seasons=1, season_weights=(1.0,), shrink_games=0.0, games_regression=0.5)
    (rec,) = src.project(season=2026)
    assert rec.stats["games"] == pytest.approx(12.5)
    assert rec.stats["rush_yds"] == pytest.approx(100.0 * 12.5)


def test_filters_postseason_other_positions_and_unlisted_history(tmp_path):
    rows = [
        week_row("00-1", "Keeper", "QB", "BUF", 2025, 1, passing_yards=300.0),
        week_row("00-2", "Kicker", "K", "BUF", 2025, 1),  # position not projectable
        week_row("00-3", "Playoff Only", "RB", "KC", 2025, 20, rushing_yards=200.0) | {"season_type": "POST"},
        week_row("00-4", "Old Timer", "RB", "CHI", 2019, 1, rushing_yards=100.0),  # season outside history window
    ]
    src = make_source(rows, tmp_path, history_seasons=1, season_weights=(1.0,), shrink_games=0.0)
    records = src.project(season=2026)
    assert [r.source_id for r in records] == ["00-1"]


def test_emits_full_component_stat_line_keys(tmp_path):
    rows = [week_row("00-1", "QB Guy", "QB", "PHI", 2025, 1, passing_yards=300.0)]
    src = make_source(rows, tmp_path, history_seasons=1, season_weights=(1.0,))
    (rec,) = src.project(season=2026)
    assert set(rec.stats) == set(PROJECTED_STAT_KEYS) | {"games"}


def test_fumbles_and_two_pt_columns_are_summed(tmp_path):
    rows = [
        week_row(
            "00-1",
            "Busy",
            "RB",
            "DET",
            2025,
            1,
            rushing_fumbles_lost=1.0,
            receiving_fumbles_lost=1.0,
            rushing_2pt_conversions=1.0,
            receiving_2pt_conversions=1.0,
        )
    ]
    src = make_source(rows, tmp_path, history_seasons=1, season_weights=(1.0,), shrink_games=0.0, games_regression=1.0)
    (rec,) = src.project(season=2026)
    assert rec.stats["fum_lost"] == pytest.approx(2.0 * 17)
    assert rec.stats["two_pt"] == pytest.approx(2.0 * 17)


def test_traded_player_shows_most_recent_team_regardless_of_row_order(tmp_path):
    # Rows arrive newest-season-first (how the fetch concatenates years); the
    # meta must still come from the latest (season, week), i.e. the new team.
    rows = [
        week_row("00-1", "Traded Guy", "RB", "PHI", 2025, 10, rushing_yards=90.0),
        week_row("00-1", "Traded Guy", "RB", "PHI", 2025, 2, rushing_yards=80.0),
        week_row("00-1", "Traded Guy", "RB", "NYG", 2023, 17, rushing_yards=70.0),
    ]
    src = make_source(rows, tmp_path, history_seasons=3, season_weights=(1.0, 1.0, 1.0))
    (rec,) = src.project(season=2026)
    assert rec.team == "PHI"


def test_records_sorted_by_points_descending(tmp_path):
    rows = [
        week_row("00-LO", "Low", "WR", "CAR", 2025, 1, receiving_yards=30.0),
        week_row("00-HI", "High", "WR", "CIN", 2025, 1, receiving_yards=130.0),
    ]
    src = make_source(rows, tmp_path, history_seasons=1, season_weights=(1.0,), shrink_games=0.0)
    records = src.project(season=2026)
    assert [r.source_id for r in records] == ["00-HI", "00-LO"]


# -- protocol + interface -----------------------------------------------------


def test_satisfies_projection_source_protocol(tmp_path):
    src = make_source([], tmp_path)
    assert isinstance(src, ProjectionSource)
    assert src.is_live


def test_week_horizon_rejected(tmp_path):
    src = make_source([], tmp_path)
    with pytest.raises(ValueError, match="season-horizon"):
        src.project(week=3)


# -- cache / warm path (framework §7 "pre-draft warm cache") -------------------


def test_project_computes_once_then_serves_from_cache(tmp_path):
    calls = []

    def fetch(years):
        calls.append(years)
        return [week_row("00-1", "Cached", "TE", "LV", 2025, 1, receiving_yards=80.0)]

    src = NflverseProjectionSource(
        nflverse=NflverseSource(fetchers={"weekly": fetch}), cache_dir=tmp_path / "cache"
    )
    first = src.project(season=2026)
    second = src.project(season=2026)
    assert len(calls) == 1  # second call never touched the fetcher
    assert [(r.source_id, r.points, r.stats) for r in first] == [
        (r.source_id, r.points, r.stats) for r in second
    ]


def test_warmed_cache_survives_offline_draft_day(tmp_path):
    cache_dir = tmp_path / "cache"
    rows = [week_row("00-1", "Warm", "WR", "SEA", 2025, 1, receiving_yards=90.0)]
    warm = NflverseProjectionSource(
        nflverse=NflverseSource(fetchers={"weekly": lambda years: rows}), cache_dir=cache_dir
    )
    warmed = warm.warm_cache(season=2026)

    def network_down(years):
        raise ConnectionError("no wifi at the draft party")

    offline = NflverseProjectionSource(
        nflverse=NflverseSource(fetchers={"weekly": network_down}), cache_dir=cache_dir
    )
    served = offline.project(season=2026)
    assert [r.source_id for r in served] == [r.source_id for r in warmed]
    assert served[0].stats == warmed[0].stats


def test_no_cache_and_fetch_failure_raises_actionable_error(tmp_path):
    def network_down(years):
        raise ConnectionError("offline")

    src = NflverseProjectionSource(
        nflverse=NflverseSource(fetchers={"weekly": network_down}), cache_dir=tmp_path / "cache"
    )
    with pytest.raises(RuntimeError, match="warm_cache"):
        src.project(season=2026)


def test_warm_cache_refreshes_stale_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    v1 = [week_row("00-1", "V1", "RB", "ATL", 2025, 1, rushing_yards=50.0)]
    v2 = [week_row("00-1", "V2", "RB", "ATL", 2025, 1, rushing_yards=150.0)]
    NflverseProjectionSource(
        nflverse=NflverseSource(fetchers={"weekly": lambda years: v1}), cache_dir=cache_dir
    ).warm_cache(season=2026)
    NflverseProjectionSource(
        nflverse=NflverseSource(fetchers={"weekly": lambda years: v2}), cache_dir=cache_dir
    ).warm_cache(season=2026)
    reader = NflverseProjectionSource(
        nflverse=NflverseSource(fetchers={"weekly": lambda years: []}), cache_dir=cache_dir
    )
    (rec,) = reader.project(season=2026)
    assert rec.name == "V2"


def test_corrupt_cache_falls_back_to_live_compute(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    src = NflverseProjectionSource(
        nflverse=NflverseSource(
            fetchers={"weekly": lambda years: [week_row("00-1", "Fresh", "WR", "TB", 2025, 1, receiving_yards=70.0)]}
        ),
        cache_dir=cache_dir,
    )
    src._cache_path(2026).write_text("{not json", encoding="utf-8")
    (rec,) = src.project(season=2026)
    assert rec.name == "Fresh"


# -- nflverse stale-URL fallback (2025 schema move) ---------------------------


def test_weekly_stats_falls_back_to_direct_parquet_on_stale_library_url(monkeypatch):
    # The real nfl_data_py path 404s for 2025+ (nflverse retired the old asset);
    # weekly_stats must fall through to the direct stats_player parquet read.
    from urllib.error import HTTPError

    src = NflverseSource()  # no injected fetchers -> fallback is allowed

    def stale_library(logical, *args, **kwargs):
        raise HTTPError("https://old-url", 404, "Not Found", None, None)

    monkeypatch.setattr(src, "_fetch", stale_library)
    monkeypatch.setattr(
        NflverseSource, "_weekly_stats_direct", staticmethod(lambda years: [{"player_id": "x", "years": list(years)}])
    )
    assert src.weekly_stats([2025, 2024]) == [{"player_id": "x", "years": [2025, 2024]}]


def test_weekly_stats_injected_fetcher_errors_are_never_swallowed():
    from urllib.error import HTTPError

    def fetch(years):
        raise HTTPError("https://x", 404, "Not Found", None, None)

    src = NflverseSource(fetchers={"weekly": fetch})
    with pytest.raises(HTTPError):  # offline tests must fail loudly, not hit the network
        src.weekly_stats([2025])


def test_new_schema_columns_are_understood(tmp_path):
    # A 2025-schema row: "team" + "passing_interceptions" instead of the old names.
    row = {
        "player_id": "00-N",
        "player_display_name": "New Schema QB",
        "position": "QB",
        "team": "HOU",
        "season": 2025,
        "week": 1,
        "season_type": "REG",
        "passing_yards": 250.0,
        "passing_interceptions": 2.0,
    }
    src = make_source([row], tmp_path, history_seasons=1, season_weights=(1.0,), shrink_games=0.0, games_regression=1.0)
    (rec,) = src.project(season=2026)
    assert rec.team == "HOU"
    assert rec.stats["pass_yds"] == pytest.approx(250.0 * 17)
    assert rec.stats["pass_int"] == pytest.approx(2.0 * 17)


# -- source selection (free vs paid) ------------------------------------------


def test_factory_defaults_to_free_consensus_blend():
    # Still free and keyless — the default is the model anchored by market ADP.
    from fantasy_coach.ingest.consensus import ConsensusProjectionSource

    config = Config.load(environ={})
    src = make_projection_source(config)
    assert isinstance(src, ConsensusProjectionSource)
    assert isinstance(src.model, NflverseProjectionSource)
    assert src.cache_dir == Path(".cache")


def test_factory_nflverse_selects_raw_model():
    config = Config.load(environ={"PROJECTION_SOURCE": "nflverse"})
    src = make_projection_source(config)
    assert isinstance(src, NflverseProjectionSource)
    assert src.cache_dir == Path(".cache")


def test_factory_respects_env_selection_and_cache_dir(tmp_path):
    config = Config.load(
        environ={
            "PROJECTION_SOURCE": "fantasypros",
            "FANTASYPROS_API_KEY": "key-123",
            "FANTASY_COACH_CACHE_DIR": str(tmp_path),
        }
    )
    src = make_projection_source(config)
    assert isinstance(src, FantasyProsSource)
    assert src.api_key == "key-123"

    config_free = Config.load(
        environ={
            "PROJECTION_SOURCE": "nflverse",
            "FANTASY_COACH_CACHE_DIR": str(tmp_path),
        }
    )
    free = make_projection_source(config_free)
    assert isinstance(free, NflverseProjectionSource)
    assert free.cache_dir == tmp_path


def test_factory_rejects_unknown_source():
    config = Config.load(environ={"PROJECTION_SOURCE": "psychic-hotline"})
    with pytest.raises(ValueError, match="PROJECTION_SOURCE"):
        make_projection_source(config)


def test_reference_scoring_covers_every_stat_key():
    # Every emitted component stat is scoreable (games is intentionally not).
    assert set(REFERENCE_SCORING) == set(PROJECTED_STAT_KEYS)
