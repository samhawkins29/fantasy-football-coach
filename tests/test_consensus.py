"""Tests for the consensus projection blend (enhancement 1).

All offline and hand-computable, the projections-test pattern: the anchor model
is a fake with pinned records, the ADP→points calibration is injectable so the
blend math checks against exact numbers, and the real fitter is proven on
colinear points where least squares is exact.

The load-bearing contracts:

* blend = weighted mean of the signals actually present, renormalized;
* single-signal players pass through untouched (never dropped, never invented);
* a missing/dead source degrades with a warning, never an error;
* the consensus record shape feeds ``build_value_board`` unchanged;
* **off-by-default**: the default configuration selects the plain nflverse
  model and produces a board identical to the certified base.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from fantasy_coach.clients.models import LeagueSettings, RosterPosition, StatCategory
from fantasy_coach.config import Config
from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
from fantasy_coach.ingest.consensus import (
    CONSENSUS_NOTE,
    ConsensusProjectionSource,
    fit_log_points_curve,
    market_adp_from_players,
)
from fantasy_coach.ingest.projections import (
    NflverseProjectionSource,
    make_projection_source,
)
from fantasy_coach.ingest.sources import (
    FantasyProsSource,
    NflverseSource,
    ProjectionRecord,
    ProjectionSource,
    SourceNotConfigured,
)
from fantasy_coach.value.board import SOURCE_ADP, SOURCE_PROJECTION, build_value_board

HALF_PPR = {4: 0.04, 5: 4.0, 6: -1.0, 9: 0.1, 10: 6.0, 11: 0.5, 12: 0.1, 13: 6.0, 16: 2.0, 18: -2.0}


def mrec(gsis: str, name: str, position: str, points: float) -> ProjectionRecord:
    """A model record whose stats rescore to exactly ``points`` under half-PPR
    (rec_yds at 0.1/yd), so stat-scaling effects are checkable on paper."""
    return ProjectionRecord(
        source="fake_model",
        source_id=gsis,
        source_id_field="gsis_id",
        points=points,
        position=position,
        team="KC",
        name=name,
        stats={"rec_yds": round(points * 10.0, 2), "games": 17.0},
    )


@dataclass
class FakeModel:
    """A pinned-records anchor model (the ProjectionSource fixture)."""

    records: list[ProjectionRecord] = field(default_factory=list)
    fail: bool = False
    name: str = "fake_model"
    calls: int = 0
    warmed: int = 0

    @property
    def is_live(self) -> bool:
        return True

    def project(self, *, week=None, season=None):
        self.calls += 1
        if self.fail:
            raise ConnectionError("model offline")
        return [
            ProjectionRecord(**{f: getattr(r, f) for f in (
                "source", "source_id", "source_id_field", "points",
                "position", "team", "name",
            )}, stats=dict(r.stats))
            for r in self.records
        ]

    def warm_cache(self, season=None):
        self.warmed += 1
        return self.project(season=season)


def flat_curve(value: float):
    """A curve_fitter that always implies ``value`` points, whatever the ADP."""
    return lambda pairs: (lambda adp: value)


def make_consensus(tmp_path: Path, **kwargs) -> ConsensusProjectionSource:
    kwargs.setdefault("cache_dir", tmp_path / "cache")
    return ConsensusProjectionSource(**kwargs)


# -- the calibration fit (pure math) ------------------------------------------


def test_fit_log_points_curve_exact_on_colinear_points():
    # points = 300 - 50·ln(adp): least squares through colinear points is exact.
    pairs = [(1.0, 300.0), (math.e, 250.0), (math.e**2, 200.0)]
    curve = fit_log_points_curve(pairs)
    assert curve is not None
    assert curve(1.0) == pytest.approx(300.0)
    assert curve(math.e**3) == pytest.approx(150.0)


def test_fit_log_points_curve_degenerate_inputs_return_none():
    assert fit_log_points_curve([]) is None
    assert fit_log_points_curve([(5.0, 100.0), (5.0, 120.0)]) is None
    # ADPs below 1 clamp to 1 — still a single distinct x.
    assert fit_log_points_curve([(0.2, 100.0), (1.0, 120.0)]) is None


# -- blend math (hand-computable via an injected curve) ------------------------


def test_blend_is_weighted_mean_and_scales_stats():
    # Model 200, market-implied 300, weights 0.5/0.5 -> 250; stats scale by
    # 250/200 = 1.25 (rec_yds 2000 -> 2500), games untouched.
    src = make_consensus(
        Path("unused"),
        model=FakeModel([mrec("00-D", "Blend Me", "WR", 200.0)]),
        market={"00-D": 12.0},
        weights={"model": 0.5, "market": 0.5},
        curve_fitter=flat_curve(300.0),
    )
    (rec,) = src._compute(2026)
    assert rec.points == pytest.approx(250.0)
    assert rec.stats["rec_yds"] == pytest.approx(2500.0)
    assert rec.stats["games"] == pytest.approx(17.0)
    assert rec.inputs == ("model", "market")
    assert rec.source == "consensus"


def test_default_weights_anchor_on_the_model():
    # 0.6·200 + 0.4·300 = 240 — the model anchors, the market corrects.
    src = make_consensus(
        Path("unused"),
        model=FakeModel([mrec("00-D", "Blend Me", "WR", 200.0)]),
        market={"00-D": 12.0},
        curve_fitter=flat_curve(300.0),
    )
    (rec,) = src._compute(2026)
    assert rec.points == pytest.approx(240.0)


def test_weights_renormalize_over_present_signals():
    # Market-only weight: blended == the market number, stats scaled 1.5.
    src = make_consensus(
        Path("unused"),
        model=FakeModel([mrec("00-D", "Market Darling", "WR", 200.0)]),
        market={"00-D": 3.0},
        weights={"model": 0.0, "market": 1.0},
        curve_fitter=flat_curve(300.0),
    )
    (rec,) = src._compute(2026)
    assert rec.points == pytest.approx(300.0)
    assert rec.stats["rec_yds"] == pytest.approx(3000.0)


def test_model_only_weights_leave_records_bit_identical():
    # weights model=1/market=0: the blend is arithmetically the model — points
    # and stats unchanged (the label still says market was consulted).
    base = mrec("00-D", "Anchored", "WR", 187.35)
    src = make_consensus(
        Path("unused"),
        model=FakeModel([base]),
        market={"00-D": 20.0},
        weights={"model": 1.0, "market": 0.0},
        curve_fitter=flat_curve(999.0),
    )
    (rec,) = src._compute(2026)
    assert rec.points == base.points
    assert rec.stats == base.stats
    assert rec.inputs == ("model", "market")


# -- single-signal fallthrough + degradation -----------------------------------


def test_player_without_adp_passes_through_untouched():
    with_adp = mrec("00-A", "Market Knows", "WR", 200.0)
    without = mrec("00-B", "Model Only", "WR", 150.0)
    src = make_consensus(
        Path("unused"),
        model=FakeModel([with_adp, without]),
        market={"00-A": 5.0},
        curve_fitter=flat_curve(300.0),
    )
    by_id = {r.source_id: r for r in src._compute(2026)}
    assert by_id["00-B"].points == without.points
    assert by_id["00-B"].stats == without.stats
    assert by_id["00-B"].inputs == ("model",)
    assert by_id["00-A"].inputs == ("model", "market")


def test_no_market_at_all_degrades_to_model_with_warning(caplog):
    records = [mrec("00-A", "A", "WR", 200.0), mrec("00-B", "B", "RB", 150.0)]
    src = make_consensus(Path("unused"), model=FakeModel(records), market=None)
    with caplog.at_level("WARNING", logger="fantasy_coach.ingest.consensus"):
        out = src._compute(2026)
    assert [(r.source_id, r.points, r.stats) for r in out] == [
        (r.source_id, r.points, r.stats) for r in records
    ]
    assert all(r.inputs == ("model",) for r in out)
    assert any("no market ADP" in m for m in caplog.messages)


def test_market_only_players_are_not_emitted():
    # An ADP with no model stat line stays with the board's own ADP gap-fill
    # (league-scored VORP space) instead of becoming a fake stat projection.
    src = make_consensus(
        Path("unused"),
        model=FakeModel([mrec("00-A", "Projected", "WR", 200.0)]),
        market={"00-A": 5.0, "00-ROOKIE": 18.0},
        curve_fitter=flat_curve(300.0),
    )
    assert [r.source_id for r in src._compute(2026)] == ["00-A"]


def test_nonpositive_model_points_never_blend_or_scale():
    zero = mrec("00-Z", "Zero", "WR", 0.0)
    src = make_consensus(
        Path("unused"),
        model=FakeModel([zero]),
        market={"00-Z": 10.0},
        curve_fitter=flat_curve(300.0),
    )
    (rec,) = src._compute(2026)
    assert rec.points == 0.0
    assert rec.inputs == ("model",)


def test_market_is_lazily_callable():
    src = make_consensus(
        Path("unused"),
        model=FakeModel([mrec("00-A", "Lazy", "WR", 200.0)]),
        market=lambda: {"00-A": 5.0},
        weights={"model": 0.5, "market": 0.5},
        curve_fitter=flat_curve(100.0),
    )
    (rec,) = src._compute(2026)
    assert rec.points == pytest.approx(150.0)


# -- calibration wiring (real fitter + positional fallback) --------------------


def test_market_on_the_models_line_moves_nothing():
    # Four WRs exactly on points = 300 - 50·ln(adp): the calibrated market
    # agrees with the model everywhere, so the consensus IS the model.
    records = [
        mrec("00-1", "P1", "WR", 300.0),
        mrec("00-2", "P2", "WR", 250.0),
        mrec("00-3", "P3", "WR", 200.0),
        mrec("00-4", "P4", "WR", 150.0),
    ]
    market = {"00-1": 1.0, "00-2": math.e, "00-3": math.e**2, "00-4": math.e**3}
    src = make_consensus(Path("unused"), model=FakeModel(records), market=market)
    out = {r.source_id: r for r in src._compute(2026)}
    for base in records:
        assert out[base.source_id].points == pytest.approx(base.points)


def test_thin_position_falls_back_to_global_curve():
    # WRs (3 priced players) use rank calibration; the lone RB has too few
    # positional pairs (min 3) and must use the injected global curve.
    def counting_fitter(pairs):
        return lambda adp, n=len(pairs): 100.0 * n

    records = [
        mrec("00-1", "W1", "WR", 100.0),
        mrec("00-2", "W2", "WR", 100.0),
        mrec("00-3", "W3", "WR", 100.0),
        mrec("00-9", "Lone RB", "RB", 100.0),
    ]
    market = {"00-1": 2.0, "00-2": 3.0, "00-3": 4.0, "00-9": 5.0}
    src = make_consensus(
        Path("unused"),
        model=FakeModel(records),
        market=market,
        weights={"model": 0.5, "market": 0.5},
        curve_fitter=counting_fitter,
    )
    out = {r.source_id: r for r in src._compute(2026)}
    # WR rank calibration: all three WRs share 100 model points, so the
    # market signal IS 100 and the blend is the identity.
    assert out["00-1"].points == pytest.approx(100.0)
    # The lone RB uses the global fit over all 4 pairs → market 400.
    assert out["00-9"].points == pytest.approx((100.0 + 400.0) / 2)


def test_rank_calibration_adopts_market_order_on_model_scale():
    # The model loves W-OLD (300) and dismisses W-BREAKOUT (150); the market
    # drafts W-BREAKOUT first. Rank calibration hands W-BREAKOUT the model's
    # best WR points (300) as its market signal — and the huge model-vs-market
    # rank gap escalates the market share, so the breakout lands near the
    # market's view, not the model's.
    records = [
        mrec("00-OLD", "W Old", "WR", 300.0),
        mrec("00-MID", "W Mid", "WR", 220.0),
        mrec("00-BRK", "W Breakout", "WR", 150.0),
    ]
    market = {"00-BRK": 1.0, "00-MID": 20.0, "00-OLD": 40.0}
    src = make_consensus(
        Path("unused"), model=FakeModel(records), market=market,
        weights={"model": 0.6, "market": 0.4},
    )
    out = {r.source_id: r for r in src._compute(2026)}
    # Breakout: market signal 300, rank gap 2/3 → share escalates to the
    # 0.85 cap → 0.15×150 + 0.85×300 = 277.5.
    assert out["00-BRK"].points == pytest.approx(277.5)
    # Old: market signal 150 (market's 3rd WR), same escalation downward:
    # 0.15×300 + 0.85×150 = 172.5 — the market demotion actually bites.
    assert out["00-OLD"].points == pytest.approx(172.5)
    # Mid agrees with the market (rank 2 both ways): base 0.4 share, signal
    # = his own 220 → identity.
    assert out["00-MID"].points == pytest.approx(220.0)


# -- the pluggable extra slot (FantasyPros later) ------------------------------


@dataclass
class FakeExtra:
    """A stand-in for a keyed FantasyPros-style source."""

    name: str = "fantasypros"
    points_by_gsis: dict = field(default_factory=dict)
    live: bool = True
    explode: bool = False
    id_field: str = "gsis_id"

    @property
    def is_live(self) -> bool:
        return self.live

    def project(self, *, week=None, season=None):
        if self.explode:
            raise SourceNotConfigured("boom")
        return [
            ProjectionRecord(
                source=self.name, source_id=g, source_id_field=self.id_field, points=p
            )
            for g, p in self.points_by_gsis.items()
        ]


def test_extra_source_joins_the_blend():
    src = make_consensus(
        Path("unused"),
        model=FakeModel([mrec("00-A", "Threeway", "WR", 200.0)]),
        market=None,
        extra_sources=(FakeExtra(points_by_gsis={"00-A": 100.0}),),
        weights={"model": 1.0, "fantasypros": 1.0},
    )
    (rec,) = src._compute(2026)
    assert rec.points == pytest.approx(150.0)
    assert rec.inputs == ("model", "fantasypros")


def test_dead_or_exploding_extra_source_is_skipped(caplog):
    base = mrec("00-A", "Solo", "WR", 200.0)
    for extra in (FakeExtra(live=False), FakeExtra(explode=True)):
        src = make_consensus(
            Path("unused"), model=FakeModel([base]), market=None, extra_sources=(extra,)
        )
        with caplog.at_level("WARNING", logger="fantasy_coach.ingest.consensus"):
            (rec,) = src._compute(2026)
        assert rec.points == base.points
        assert rec.inputs == ("model",)
    assert any("skipped" in m for m in caplog.messages)


def test_extra_records_not_keyed_by_gsis_are_ignored(caplog):
    base = mrec("00-A", "Solo", "WR", 200.0)
    extra = FakeExtra(points_by_gsis={"fp123": 100.0}, id_field="fantasypros_id")
    src = make_consensus(
        Path("unused"), model=FakeModel([base]), market=None, extra_sources=(extra,)
    )
    with caplog.at_level("WARNING", logger="fantasy_coach.ingest.consensus"):
        (rec,) = src._compute(2026)
    assert rec.inputs == ("model",)
    assert any("crosswalk" in m for m in caplog.messages)


# -- protocol + cache (zero network on draft day) ------------------------------


def test_satisfies_projection_source_protocol(tmp_path):
    src = make_consensus(tmp_path, model=FakeModel([]))
    assert isinstance(src, ProjectionSource)
    assert src.is_live


def test_week_horizon_rejected(tmp_path):
    with pytest.raises(ValueError, match="season-horizon"):
        make_consensus(tmp_path, model=FakeModel([])).project(week=3)


def test_project_computes_once_then_serves_from_cache(tmp_path):
    model = FakeModel([mrec("00-A", "Cached", "WR", 200.0)])
    src = make_consensus(tmp_path, model=model, market={"00-A": 5.0}, curve_fitter=flat_curve(300.0))
    first = src.project(season=2026)
    second = src.project(season=2026)
    assert model.calls == 1
    assert [(r.source_id, r.points, r.stats, r.inputs) for r in first] == [
        (r.source_id, r.points, r.stats, r.inputs) for r in second
    ]


def test_warmed_cache_survives_offline_draft_day(tmp_path):
    model = FakeModel([mrec("00-A", "Warm", "WR", 200.0)])
    warm = make_consensus(
        tmp_path,
        model=model,
        market={"00-A": 5.0},
        weights={"model": 0.5, "market": 0.5},
        curve_fitter=flat_curve(300.0),
    )
    warmed = warm.warm_cache(season=2026)
    assert model.warmed == 1  # the anchor model was refreshed too

    offline = make_consensus(tmp_path, model=FakeModel(fail=True))
    served = offline.project(season=2026)
    assert [(r.source_id, r.points, r.inputs) for r in served] == [
        (r.source_id, r.points, r.inputs) for r in warmed
    ]
    assert served[0].points == pytest.approx(250.0)


def test_no_cache_and_dead_model_raises_actionable_error(tmp_path):
    src = make_consensus(tmp_path, model=FakeModel(fail=True))
    with pytest.raises(RuntimeError, match="warm_cache"):
        src.project(season=2026)


def test_corrupt_cache_falls_back_to_live_compute(tmp_path):
    src = make_consensus(tmp_path, model=FakeModel([mrec("00-A", "Fresh", "WR", 100.0)]))
    src.cache_dir.mkdir(parents=True)
    src._cache_path(2026).write_text("{not json", encoding="utf-8")
    (rec,) = src.project(season=2026)
    assert rec.name == "Fresh"


def test_model_warm_failure_degrades_to_model_cache(tmp_path, caplog):
    class WarmExploder(FakeModel):
        def warm_cache(self, season=None):
            raise ConnectionError("nflverse down")

    model = WarmExploder([mrec("00-A", "Still Here", "WR", 100.0)])
    src = make_consensus(tmp_path, model=model)
    with caplog.at_level("WARNING", logger="fantasy_coach.ingest.consensus"):
        (rec,) = src.warm_cache(season=2026)
    assert rec.name == "Still Here"  # blend fell back to model.project()
    assert any("warm_cache failed" in m for m in caplog.messages)


# -- market helper -------------------------------------------------------------


def test_market_adp_from_players_filters_missing_ids_and_adp():
    def player(cid, gsis, adp):
        p = CanonicalPlayer(canonical_id=cid, ids=ExternalIds(gsis_id=gsis))
        p.market.adp = adp
        return p

    market = market_adp_from_players(
        [player("A", "00-A", 5.0), player("B", None, 9.0), player("C", "00-C", None)]
    )
    assert market == {"00-A": 5.0}


# -- factory + off-by-default safety -------------------------------------------


def test_factory_consensus_selection_weights_and_cache_dir(tmp_path):
    config = Config.load(
        environ={
            "PROJECTION_SOURCE": "consensus",
            "CONSENSUS_MODEL_WEIGHT": "0.6",
            "CONSENSUS_MARKET_WEIGHT": "0.4",
            "FANTASY_COACH_CACHE_DIR": str(tmp_path),
        }
    )
    src = make_projection_source(config, market={"00-A": 5.0})
    assert isinstance(src, ConsensusProjectionSource)
    assert src.weights == {"model": 0.6, "market": 0.4}
    assert src.cache_dir == tmp_path
    assert isinstance(src.model, NflverseProjectionSource)
    assert src.model.cache_dir == tmp_path
    assert src.extra_sources == ()  # no FantasyPros key -> no paid slot


def test_factory_consensus_wires_keyed_fantasypros_slot(tmp_path):
    config = Config.load(
        environ={
            "PROJECTION_SOURCE": "consensus",
            "FANTASYPROS_API_KEY": "key-123",
            "FANTASY_COACH_CACHE_DIR": str(tmp_path),
        }
    )
    src = make_projection_source(config)
    (extra,) = src.extra_sources
    assert isinstance(extra, FantasyProsSource)
    assert extra.is_live


def test_factory_default_is_the_consensus_blend():
    # P1-1: the DEFAULT source is now the model+market consensus — history-only
    # rankings (a breakout QB at board #1000) are corrected by the market.
    from fantasy_coach.ingest.consensus import ConsensusProjectionSource

    config = Config.load(environ={})
    src = make_projection_source(config, market={"00-A": 5.0})
    assert isinstance(src, ConsensusProjectionSource)
    assert src.weights == {"model": 0.6, "market": 0.4}


def test_factory_nflverse_ignores_market_and_stays_single_source():
    config = Config.load(environ={"PROJECTION_SOURCE": "nflverse"})
    src = make_projection_source(config, market={"00-A": 5.0})
    assert isinstance(src, NflverseProjectionSource)


def test_default_setting_board_is_unchanged_vs_base(tmp_path):
    """The off-by-default identity check: with PROJECTION_SOURCE unset, the
    factory-selected source produces a board identical to the certified base
    path — even when market ADP is on offer."""
    rows = [
        {
            "player_id": g, "player_display_name": n, "position": p,
            "recent_team": "KC", "season": 2025, "week": 1,
            "season_type": "REG", "receiving_yards": yds,
        }
        for g, n, p, yds in [
            ("00-1", "Alpha", "WR", 120.0),
            ("00-2", "Beta", "WR", 90.0),
            ("00-3", "Gamma", "RB", 70.0),
        ]
    ]
    base_source = NflverseProjectionSource(
        nflverse=NflverseSource(fetchers={"weekly": lambda years: rows}),
        cache_dir=tmp_path,
    )
    base_records = base_source.warm_cache(season=2026)  # also writes the cache

    config = Config.load(environ={"FANTASY_COACH_CACHE_DIR": str(tmp_path)})
    selected = make_projection_source(config, market={"00-1": 1.0, "00-2": 9.0})
    selected_records = selected.project(season=2026)  # cache-served, no network

    settings = LeagueSettings(
        max_teams=2,
        roster_positions=[
            RosterPosition(position="WR", count=1),
            RosterPosition(position="RB", count=1),
        ],
        stat_categories=[StatCategory(stat_id=s, value=v) for s, v in HALF_PPR.items()],
    )
    base_board = build_value_board(base_records, settings)
    selected_board = build_value_board(selected_records, settings)
    assert [
        (e.canonical_id, e.points, e.vorp, e.overall_rank, e.tier, e.value_source)
        for e in base_board.entries
    ] == [
        (e.canonical_id, e.points, e.vorp, e.overall_rank, e.tier, e.value_source)
        for e in selected_board.entries
    ]


# -- the consensus feeds the board unchanged -----------------------------------


def _board_settings() -> LeagueSettings:
    return LeagueSettings(
        max_teams=2,
        roster_positions=[
            RosterPosition(position="WR", count=2),
            RosterPosition(position="RB", count=1),
        ],
        stat_categories=[StatCategory(stat_id=s, value=v) for s, v in HALF_PPR.items()],
    )


def test_consensus_records_build_a_sane_board():
    records = [
        mrec("00-1", "Alpha", "WR", 300.0),
        mrec("00-2", "Beta", "WR", 250.0),
        mrec("00-3", "Gamma", "WR", 200.0),
        mrec("00-4", "Delta", "RB", 220.0),
        mrec("00-5", "Echo", "RB", 180.0),
    ]
    market = {"00-1": 1.0, "00-2": 3.0, "00-3": 40.0, "00-4": 2.0, "00-5": 20.0}
    src = make_consensus(Path("unused"), model=FakeModel(records), market=market)
    blended = src._compute(2026)

    players = []
    for rec in records:
        p = CanonicalPlayer(
            canonical_id=rec.source_id,
            ids=ExternalIds(gsis_id=rec.source_id),
            name=rec.name,
            position=rec.position,
        )
        p.market.adp = market[rec.source_id]
        players.append(p)
    rookie = CanonicalPlayer(canonical_id="R1", ids=ExternalIds(), name="Hot Rookie", position="WR")
    rookie.market.adp = 12.0
    players.append(rookie)

    board = build_value_board(blended, _board_settings(), players=players)
    by_id = {e.canonical_id: e for e in board.entries}
    # All blended players landed as projection-based entries with sane values.
    for rec in records:
        entry = by_id[rec.source_id]
        assert entry.value_source == SOURCE_PROJECTION
        assert entry.points is not None and 0.0 < entry.points < 500.0
    # The market-only rookie still enters via the board's own ADP gap-fill.
    assert by_id["R1"].value_source == SOURCE_ADP
    # Ranks are a permutation ordered by rank value.
    ranks = [e.overall_rank for e in board.entries]
    assert sorted(ranks) == list(range(1, len(board.entries) + 1))
    values = [e.rank_value for e in sorted(board.entries, key=lambda e: e.overall_rank)]
    assert values == sorted(values, reverse=True)


def test_warm_store_stamps_consensus_note_and_avoids_duplicate_fallback(tmp_path):
    from fantasy_coach.store import CoachStore, warm_store

    settings = LeagueSettings(
        league_key="449.l.777",
        max_teams=2,
        roster_positions=[RosterPosition(position="WR", count=1)],
        stat_categories=[StatCategory(stat_id=s, value=v) for s, v in HALF_PPR.items()],
    )
    store = CoachStore(tmp_path / "coach.sqlite3")

    # Warm once with the base model, then once with consensus: both sources'
    # rows now coexist for the season.
    base = [
        ProjectionRecord(
            source="nflverse_model", source_id=g, source_id_field="gsis_id",
            points=p, position="WR", name=n, stats={"rec_yds": p * 10.0},
        )
        for g, n, p in [("G1", "Alpha", 200.0), ("G2", "Beta", 150.0)]
    ]
    warm_store(store, settings, projections=base, season=2026)
    src = make_consensus(
        tmp_path, model=FakeModel(base), market={"G1": 1.0},
        weights={"model": 0.5, "market": 0.5}, curve_fitter=flat_curve(300.0),
    )
    warm_store(store, settings, projections=src.project(season=2026), season=2026)

    notes = {
        row["source"]: row["note"]
        for row in store.sql("SELECT DISTINCT source, note FROM projections")
    }
    assert notes["consensus"] == CONSENSUS_NOTE

    # Degraded warm (pull fails, no records given): the fallback must use only
    # the failing source's own stored rows — not double every player.
    class FailingConsensus:
        name = "consensus"
        is_live = True

        def project(self, *, week=None, season=None):
            raise RuntimeError("offline")

    result = warm_store(
        store, settings, projection_source=FailingConsensus(), season=2026
    )
    assert result.board_entries == len(base)  # one entry per player, not two
    store.close()
