"""Free season projections derived from nflverse history (M4 §4.1 input).

This is the concrete, free :class:`~fantasy_coach.ingest.sources.ProjectionSource`
the value engine consumes by default. It is deliberately a **simple, transparent
baseline** — the framework's blend (§4.1) and calibration loop (§4.5) are what
make accuracy compound; this source just guarantees there is a defensible,
no-key, no-scrape projection under everything. Every record is labelled
``"model estimate (nflverse-based)"`` so the honesty posture matches the rest of
the stack.

The model, in full (no hidden steps):

1. **Pull** the last ``history_seasons`` regular-season weekly box scores from
   nflverse (``import_weekly_data`` via :class:`NflverseSource` — lazily
   imported, fetchers injectable, so tests run offline against fixtures).
2. **Aggregate** each player's per-season totals for the component stats
   (:data:`PROJECTED_STAT_KEYS`) and games played (weeks with a stat row).
3. **Per-game rates, recency-weighted.** Rates are computed across seasons with
   ``season_weights`` (most recent first, default ``0.6/0.25/0.15``), weighted
   additionally by games played — a 17-game season counts more than a 5-game one.
4. **Regress toward positional priors.** Low-sample players are shrunk toward a
   damped positional baseline:
   ``rate = (G·player_rate + K·prior) / (G + K)`` with ``K = shrink_games``.
   The prior is the mean per-game rate of *qualified* players at the position
   (≥ ``min_prior_games`` weighted games), damped by ``prior_damping`` because
   low-sample players are typically deployed below the qualified mean.
5. **Project games**, regressing injury/short history halfway to a full season:
   ``proj_games = games_regression·17 + (1−games_regression)·historical_gps``.
6. **Season stat line** = shrunk per-game rates × projected games. ``points`` is
   a convenience total under :data:`REFERENCE_SCORING` (half-PPR); **M4 must
   rescore ``stats`` through the league's own scoring**, which is why the raw
   components are always emitted.

Honest limitations (flagged, not hidden):

* **Rookies are absent** — a player with no NFL rows has no history to project.
  The draft board must fill rookies from ADP/market signals (M4/M5 concern).
* **K and DEF are not covered** — nflverse weekly data carries no kicking or
  team-defense stat lines. Same mitigation.
* **IDPs (DL/LB/DB) are covered** from the defensive columns of nflverse's
  current ``stats_player_week`` asset (tackles, sacks, INTs, FF/FR, PD, TDs,
  safeties, blocks) — same rate model, positional priors per IDP group.
* Past production ≈ opportunity proxy; the §4.1 opportunity/environment
  adjustments happen downstream in M4, not here.

**Caching / draft-day.** Live nflverse pulls hit the network (parquet downloads).
Projections are therefore cached as JSON under ``cache_dir`` per target season:
call :meth:`NflverseProjectionSource.warm_cache` before the draft, and
:meth:`project` serves from the cache with **zero network** from then on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from fantasy_coach.ingest.names import normalize_position, normalize_team
from fantasy_coach.ingest.sources import (
    FantasyProsSource,
    NflverseSource,
    ProjectionRecord,
    ProjectionSource,
)
from fantasy_coach.ingest.variance import (
    SpreadModel,
    positional_cv_priors,
    spread_ratios,
)

__all__ = [
    "PROJECTED_STAT_KEYS",
    "REFERENCE_SCORING",
    "PROJECTION_NOTE",
    "NflverseProjectionSource",
    "score_stats",
    "default_season",
    "make_projection_source",
]

#: The component stats every projection emits (plus ``"games"``), and the
#: nflverse weekly-data columns each one sums. These are exactly the inputs a
#: standard Yahoo scoring profile needs (passing / rushing / receiving / PPR /
#: turnovers / 2-pt) — M4 rescoring maps league ``stat_modifiers`` onto these keys.
#: ``pass_int`` lists both vintages of nflverse's INT column (pre-2025 files say
#: ``interceptions``, the 2025 schema says ``passing_interceptions``); exactly
#: one exists per file, so summing acts as a coalesce.
PROJECTED_STAT_KEYS: dict[str, tuple[str, ...]] = {
    "pass_yds": ("passing_yards",),
    "pass_td": ("passing_tds",),
    "pass_int": ("interceptions", "passing_interceptions"),
    "rush_yds": ("rushing_yards",),
    "rush_td": ("rushing_tds",),
    "rec": ("receptions",),
    "rec_yds": ("receiving_yards",),
    "rec_td": ("receiving_tds",),
    "fum_lost": ("sack_fumbles_lost", "rushing_fumbles_lost", "receiving_fumbles_lost"),
    "two_pt": (
        "passing_2pt_conversions",
        "rushing_2pt_conversions",
        "receiving_2pt_conversions",
    ),
    # IDP components (nflverse ``stats_player_week`` defensive columns; the
    # legacy ``player_stats`` files lack them, so IDPs need the current asset
    # — see ``NflverseSource.weekly_stats``).
    "tackle_solo": ("def_tackles_solo",),
    "tackle_ast": ("def_tackle_assists",),
    "sack": ("def_sacks",),
    "def_int": ("def_interceptions",),
    "ff": ("def_fumbles_forced",),
    "fr": ("fumble_recovery_opp",),
    "def_td": ("def_tds",),
    "safety": ("def_safeties",),
    "pass_def": ("def_pass_defended",),
    "blk": ("def_punt_blocks", "def_pat_blocks", "def_fg_blocks"),
}

#: The IDP component keys (a subset of :data:`PROJECTED_STAT_KEYS`).
IDP_STAT_KEYS: frozenset[str] = frozenset({
    "tackle_solo", "tackle_ast", "sack", "def_int", "ff", "fr", "def_td",
    "safety", "pass_def", "blk",
})

#: Reference scoring for the convenience ``points`` total: half-PPR, 4-pt pass
#: TD — Yahoo-default-shaped. **Not** league truth; M4 rescores ``stats``.
REFERENCE_SCORING: dict[str, float] = {
    "pass_yds": 0.04,
    "pass_td": 4.0,
    "pass_int": -1.0,
    "rush_yds": 0.1,
    "rush_td": 6.0,
    "rec": 0.5,
    "rec_yds": 0.1,
    "rec_td": 6.0,
    "fum_lost": -2.0,
    "two_pt": 2.0,
    # Yahoo-default IDP scoring (a league's own modifiers override all of it).
    "tackle_solo": 1.0,
    "tackle_ast": 0.5,
    "sack": 2.0,
    "def_int": 3.0,
    "ff": 2.0,
    "fr": 2.0,
    "def_td": 6.0,
    "safety": 2.0,
    "pass_def": 1.0,
    "blk": 2.0,
}

#: The honesty label stamped into every cache file (and this source's records).
PROJECTION_NOTE = "model estimate (nflverse-based): recency-weighted per-game rates, regressed to positional priors"

#: Positions this model can project (see module docstring for why K/DEF aren't).
#: IDP groups (DL/LB/DB — nflverse sub-positions already collapsed by
#: ``normalize_position``) project through the same rate model on the
#: defensive component stats.
_PROJECTABLE_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "DL", "LB", "DB"})

#: NFL regular season length (2021+). The games ceiling and full-season anchor.
_FULL_SEASON_GAMES = 17.0


def score_stats(stats: Mapping[str, float], scoring: Mapping[str, float] | None = None) -> float:
    """Score a stat line under a per-stat point map (default :data:`REFERENCE_SCORING`).

    Pure math — the same shape M4 uses with a league's real ``ScoringProfile``.
    Keys missing from either side contribute nothing.
    """
    scoring = REFERENCE_SCORING if scoring is None else scoring
    return sum(float(stats.get(k, 0.0)) * pts for k, pts in scoring.items())


def default_season(today: date | None = None) -> int:
    """The season currently being *projected* (drafted for), from the calendar.

    From March onward that is the upcoming season in the same calendar year
    (drafts run Aug–Sep); Jan–Feb still belongs to the prior season's playoffs.
    """
    today = today or date.today()
    return today.year if today.month >= 3 else today.year - 1


def _records_of(frame: object) -> list[Mapping[str, object]]:
    """Normalize a fetcher result to a list of row mappings.

    Production hands us a ``pandas.DataFrame`` (``.to_dict("records")``); tests
    inject plain lists of dicts — same seam as the crosswalk loader.
    """
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        return to_dict("records")
    return list(frame)  # type: ignore[arg-type]


def _num(row: Mapping[str, object], column: str) -> float:
    """A numeric cell, treating missing/NaN/None as 0.0."""
    val = row.get(column)
    if val is None:
        return 0.0
    try:
        out = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return out if out == out else 0.0  # NaN != NaN


@dataclass(slots=True)
class _SeasonLine:
    """One player's aggregated totals for one season (internal).

    ``weekly_points`` keeps each week's reference-scored fantasy points — the
    raw material for the floor/ceiling spread (:mod:`fantasy_coach.ingest.variance`).
    """

    games: float = 0.0
    totals: dict[str, float] = field(default_factory=dict)
    weekly_points: list[float] = field(default_factory=list)


@dataclass(slots=True)
class NflverseProjectionSource:
    """Free season projections from nflverse history (the default ProjectionSource).

    Implements the :class:`~fantasy_coach.ingest.sources.ProjectionSource`
    protocol; records route through the crosswalk on ``gsis_id`` — nflverse's
    native id **is** the hub id (§3.1), so the join is the happy path.

    All model knobs are dataclass fields so tests can pin them to hand-computable
    values and the calibration loop (M11) can tune them later without a rewrite.

    Args:
        nflverse: The fetch layer. Tests inject ``NflverseSource(fetchers=...)``.
        history_seasons: How many completed seasons to learn from.
        season_weights: Recency weights, most recent season first. Padded/truncated
            to ``history_seasons`` entries (extra seasons get the last weight).
        shrink_games: ``K`` in the shrinkage — how many games of the positional
            prior a player's own sample must outweigh.
        prior_damping: Scale on the positional prior (< 1 because low-sample
            players are typically deployed below the qualified-player mean).
        min_prior_games: Weighted games needed to count toward the prior.
        games_regression: Weight on a full 17-game season when projecting games
            (the rest comes from the player's historical games/season).
        scoring: Point map for the convenience ``points`` field.
        cache_dir: Where per-season projection JSON caches live (git-ignored).
        spread: The floor/ceiling spread model knobs (projection
            distributions). Every record carries ``floor``/``ceiling`` on the
            reference scale; the board applies them as ratios to league points.
    """

    name: str = "nflverse_model"
    nflverse: NflverseSource = field(default_factory=NflverseSource)
    history_seasons: int = 3
    season_weights: tuple[float, ...] = (0.6, 0.25, 0.15)
    shrink_games: float = 6.0
    prior_damping: float = 0.5
    min_prior_games: float = 8.0
    games_regression: float = 0.5
    scoring: Mapping[str, float] = field(default_factory=lambda: dict(REFERENCE_SCORING))
    cache_dir: Path = field(default_factory=lambda: Path(".cache"))
    spread: SpreadModel = field(default_factory=SpreadModel)

    @property
    def is_live(self) -> bool:
        """True — nflverse needs no key (reachability is per-call, like the source)."""
        return True

    # -- ProjectionSource protocol -------------------------------------------

    def project(self, *, week: int | None = None, season: int | None = None) -> list[ProjectionRecord]:
        """Season projections for ``season`` (default: the upcoming season).

        Serves from the local cache when one exists for the season (the zero-
        network draft-day path — see :meth:`warm_cache`); otherwise computes
        live from nflverse and writes the cache as a side effect.

        Raises:
            ValueError: If ``week`` is given — v1 projects season horizon only.
            RuntimeError: If no cache exists and the live nflverse pull fails.
        """
        if week is not None:
            raise ValueError(
                "NflverseProjectionSource v1 produces season-horizon projections only "
                "(week=None); weekly projections are an M4/M6 concern."
            )
        season = season or default_season()
        cached = self._load_cache(season)
        if cached is not None:
            return cached
        try:
            records = self._compute(season)
        except Exception as exc:  # network / parquet / schema failures
            raise RuntimeError(
                f"nflverse projection pull failed for season {season} and no local "
                f"cache exists at {self._cache_path(season)}. Run warm_cache() while "
                "online (pre-draft warm cache, framework §7)."
            ) from exc
        self._write_cache(season, records)
        return records

    def warm_cache(self, season: int | None = None) -> list[ProjectionRecord]:
        """Fetch + compute + persist projections for ``season`` (pre-draft step).

        Always recomputes from a live pull (refreshing any existing cache), so
        draft day can run :meth:`project` with zero network dependency (§7
        "pre-draft warm cache").
        """
        season = season or default_season()
        records = self._compute(season)
        self._write_cache(season, records)
        return records

    # -- the model ------------------------------------------------------------

    def _history_years(self, season: int) -> list[int]:
        """Completed seasons to learn from, most recent first."""
        return [season - offset for offset in range(1, self.history_seasons + 1)]

    def _weight_for(self, recency_index: int) -> float:
        """Recency weight for the ``recency_index``-th most recent season."""
        if not self.season_weights:
            return 1.0
        return self.season_weights[min(recency_index, len(self.season_weights) - 1)]

    def _compute(self, season: int) -> list[ProjectionRecord]:
        """Run the full model against a live (or injected) nflverse weekly pull."""
        years = self._history_years(season)
        rows = _records_of(self.nflverse.weekly_stats(years))

        # 1–2. Aggregate per (player, season): games + component-stat totals.
        lines: dict[str, dict[int, _SeasonLine]] = {}
        meta: dict[str, dict[str, str]] = {}  # gsis_id -> name/position/team (most recent)
        meta_vintage: dict[str, tuple[int, int]] = {}  # gsis_id -> (season, week) meta came from
        for row in rows:
            season_type = str(row.get("season_type", "REG") or "REG")
            if season_type != "REG":
                continue
            gsis_id = str(row.get("player_id", "") or "")
            position = normalize_position(str(row.get("position", "") or ""))
            if not gsis_id or position not in _PROJECTABLE_POSITIONS:
                continue
            year = int(_num(row, "season"))
            line = lines.setdefault(gsis_id, {}).setdefault(year, _SeasonLine())
            line.games += 1.0
            week_stats: dict[str, float] = {}
            for key, columns in PROJECTED_STAT_KEYS.items():
                val = sum(_num(row, c) for c in columns)
                week_stats[key] = val
                line.totals[key] = line.totals.get(key, 0.0) + val
            line.weekly_points.append(score_stats(week_stats, self.scoring))
            # Later seasons/weeks overwrite earlier ones -> most recent name/team.
            # Name/team come from the *most recent* row by (season, week) — row
            # order can't be trusted (the fetch concatenates years newest-first),
            # and a traded player must show his latest team, not his 3-years-ago one.
            vintage = (year, int(_num(row, "week")))
            if vintage >= meta_vintage.get(gsis_id, (0, 0)):
                # "recent_team" is the pre-2025 nflverse column, "team" the 2025 schema.
                team = str(row.get("recent_team") or row.get("team") or "")
                meta[gsis_id] = {
                    "name": str(row.get("player_display_name", row.get("player_name", "")) or ""),
                    "position": position,
                    "team": normalize_team(team) or "",
                }
                meta_vintage[gsis_id] = vintage

        # 3. Recency- and games-weighted per-game rates + historical games/season.
        rates: dict[str, dict[str, float]] = {}
        weighted_games: dict[str, float] = {}
        games_per_season: dict[str, float] = {}
        for gsis_id, by_year in lines.items():
            w_games = 0.0
            w_totals: dict[str, float] = {}
            w_sum = 0.0
            w_gps = 0.0
            for recency, year in enumerate(years):
                line = by_year.get(year)
                if line is None or line.games <= 0:
                    continue
                w = self._weight_for(recency)
                w_games += w * line.games
                w_sum += w
                w_gps += w * line.games
                for key, total in line.totals.items():
                    w_totals[key] = w_totals.get(key, 0.0) + w * total
            if w_games <= 0:
                continue
            rates[gsis_id] = {k: t / w_games for k, t in w_totals.items()}
            weighted_games[gsis_id] = w_games
            games_per_season[gsis_id] = w_gps / w_sum  # avg games in seasons played

        # 4a. Positional priors: mean per-game rate of qualified players.
        prior_sums: dict[str, dict[str, float]] = {}
        prior_counts: dict[str, int] = {}
        for gsis_id, rate in rates.items():
            if weighted_games[gsis_id] < self.min_prior_games:
                continue
            pos = meta[gsis_id]["position"]
            sums = prior_sums.setdefault(pos, {})
            prior_counts[pos] = prior_counts.get(pos, 0) + 1
            for key in PROJECTED_STAT_KEYS:
                sums[key] = sums.get(key, 0.0) + rate.get(key, 0.0)
        priors = {
            pos: {k: self.prior_damping * (v / prior_counts[pos]) for k, v in sums.items()}
            for pos, sums in prior_sums.items()
        }

        # 4c. Weekly volatility priors per position (floor/ceiling input): the
        # history window's weekly points per player.
        weekly_by_player = {
            gsis_id: [
                pts
                for year in years
                if year in by_year
                for pts in by_year[year].weekly_points
            ]
            for gsis_id, by_year in lines.items()
        }
        cv_priors = positional_cv_priors(
            weekly_by_player, {gid: m["position"] for gid, m in meta.items()}
        )

        # 4b–6. Shrink, project games, emit season stat lines (+ floor/ceiling).
        records: list[ProjectionRecord] = []
        for gsis_id, rate in rates.items():
            info = meta[gsis_id]
            prior = priors.get(info["position"], {})
            g = weighted_games[gsis_id]
            k = self.shrink_games
            proj_games = min(
                _FULL_SEASON_GAMES,
                self.games_regression * _FULL_SEASON_GAMES
                + (1.0 - self.games_regression) * games_per_season[gsis_id],
            )
            stats = {
                key: round(
                    ((g * rate.get(key, 0.0) + k * prior.get(key, 0.0)) / (g + k)) * proj_games,
                    2,
                )
                for key in PROJECTED_STAT_KEYS
            }
            stats["games"] = round(proj_games, 2)
            points = round(score_stats(stats, self.scoring), 2)
            est = spread_ratios(
                weekly_by_player.get(gsis_id, []),
                position=info["position"],
                proj_games=proj_games,
                shrink_weight=k / (g + k) if (g + k) > 0 else 1.0,
                prior_cv=cv_priors.get(info["position"]),
                model=self.spread,
            )
            records.append(
                ProjectionRecord(
                    source=self.name,
                    source_id=gsis_id,
                    source_id_field="gsis_id",
                    points=points,
                    floor=round(points * est.floor_ratio, 2),
                    ceiling=round(points * est.ceiling_ratio, 2),
                    position=info["position"],
                    team=info["team"],
                    name=info["name"],
                    stats=stats,
                )
            )
        records.sort(key=lambda r: r.points, reverse=True)
        return records

    # -- cache (framework §7 "pre-draft warm cache") --------------------------

    def _cache_path(self, season: int) -> Path:
        return self.cache_dir / f"projections_{self.name}_{season}.json"

    def _write_cache(self, season: int, records: list[ProjectionRecord]) -> None:
        """Persist projections as JSON, stamped with vintage + the honesty note."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": self.name,
            "note": PROJECTION_NOTE,
            "season": season,
            "history_seasons": self._history_years(season),
            "generated_on": date.today().isoformat(),
            "records": [
                {
                    "source_id": r.source_id,
                    "source_id_field": r.source_id_field,
                    "points": r.points,
                    "floor": r.floor,
                    "ceiling": r.ceiling,
                    "position": r.position,
                    "team": r.team,
                    "name": r.name,
                    "stats": r.stats,
                }
                for r in records
            ],
        }
        self._cache_path(season).write_text(json.dumps(payload), encoding="utf-8")

    def _load_cache(self, season: int) -> list[ProjectionRecord] | None:
        """Load a season's cached projections, or ``None`` if absent/unreadable."""
        path = self._cache_path(season)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [
                ProjectionRecord(
                    source=self.name,
                    source_id=str(r["source_id"]),
                    source_id_field=str(r.get("source_id_field", "gsis_id")),
                    points=float(r["points"]),
                    floor=_opt_float(r.get("floor")),
                    ceiling=_opt_float(r.get("ceiling")),
                    position=str(r.get("position", "")),
                    team=str(r.get("team", "")),
                    name=str(r.get("name", "")),
                    stats={k: float(v) for k, v in dict(r.get("stats", {})).items()},
                )
                for r in payload.get("records", [])
            ]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None  # corrupt cache -> treat as absent, recompute live


def _opt_float(value: object) -> float | None:
    """``float(value)`` or ``None`` for a missing/null cache cell."""
    return None if value is None else float(value)  # type: ignore[arg-type]


def make_projection_source(config: object, *, market: object = None) -> ProjectionSource:
    """Build the configured :class:`ProjectionSource` (framework §2.5 free-first).

    Selection comes from ``Config.projection_source`` (env ``PROJECTION_SOURCE``):
    ``"nflverse"`` (default, free), ``"consensus"`` (enhancement 1 — blends the
    nflverse model with market-implied ADP points, weights from
    ``CONSENSUS_MODEL_WEIGHT``/``CONSENSUS_MARKET_WEIGHT``), or ``"fantasypros"``
    (needs an API key — slots in later with zero downstream changes, which is
    the point of the protocol).

    ``market`` is the consensus blend's ADP input — ``{gsis_id: adp}`` or a
    lazy zero-arg callable returning one (see
    :func:`~fantasy_coach.ingest.consensus.market_adp_from_players`). Ignored
    by the single-source kinds, so callers can always pass it.
    """
    kind = str(getattr(config, "projection_source", "") or "nflverse").strip().lower()
    cache_dir = getattr(config, "cache_dir", None)
    if kind in ("nflverse", "free"):
        if cache_dir is not None:
            return NflverseProjectionSource(cache_dir=Path(cache_dir))
        return NflverseProjectionSource()
    if kind == "consensus":
        # Lazy import: the consensus module imports this one (the model anchor).
        from fantasy_coach.ingest.consensus import ConsensusProjectionSource  # noqa: PLC0415

        kwargs: dict[str, object] = {}
        if cache_dir is not None:
            kwargs["model"] = NflverseProjectionSource(cache_dir=Path(cache_dir))
            kwargs["cache_dir"] = Path(cache_dir)
        fp_key = str(getattr(config, "fantasypros_api_key", "") or "")
        if fp_key:
            # The paid slot: keyed FantasyPros joins the blend (records must be
            # gsis-keyed to contribute; unresolved ones are skipped with a warning).
            kwargs["extra_sources"] = (FantasyProsSource(api_key=fp_key),)
        return ConsensusProjectionSource(
            market=market,
            weights={
                "model": float(getattr(config, "consensus_model_weight", 0.7)),
                "market": float(getattr(config, "consensus_market_weight", 0.3)),
            },
            **kwargs,  # type: ignore[arg-type]
        )
    if kind == "fantasypros":
        return FantasyProsSource(api_key=str(getattr(config, "fantasypros_api_key", "") or ""))
    raise ValueError(
        f"Unknown PROJECTION_SOURCE {kind!r} — expected 'nflverse' (free, default), "
        "'consensus' (model+market blend), or 'fantasypros'."
    )
