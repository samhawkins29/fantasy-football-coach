"""Consensus projections — blend the model with the market (framework §4.1 step 2).

Enhancement 1 (post-certification): instead of trusting the single nflverse
model, blend every *available, ToS-clean* projection signal into one weighted
consensus per player. Better inputs improve every downstream layer — VORP,
playoff value, injury shading — without any of them changing.

**What is honestly available to blend (and what is not):**

* **The nflverse model** (:class:`~fantasy_coach.ingest.projections.NflverseProjectionSource`)
  — free, open data, our own transparent math. Always the anchor.
* **Market-implied expectation from ADP** — Yahoo ``draft_analysis`` ADP is
  already ingested through our authorized API access. The field's collective
  draft-position wisdom is a legitimate *independent* estimate of season value;
  we calibrate ``ADP → points`` on the players the model can project (the same
  log-curve idea the board already uses for its ADP gap-fill, but in points
  space and per position) and read the market's implied points off that curve.
* **FantasyPros consensus** — a genuinely great input, but partner-API/paid.
  The slot stays pluggable (``extra_sources``): drop a keyed
  :class:`~fantasy_coach.ingest.sources.FantasyProsSource` (adapted to emit
  gsis-keyed records) into the blend and nothing downstream changes.
* **Sleeper** — exposes players/trending/status (all used elsewhere) but **no
  documented projections endpoint**; the projection URLs floating around are
  unofficial/undocumented, which is exactly the fragile-scrape posture the
  framework forbids (§1.2.6). Not blended.
* **ESPN / CBS / NFL.com scraping** — ToS gray-to-forbidden. Not used, ever.

**The blend, in full (no hidden steps):**

1. Pull the model's records (cache-served offline, like everything else).
2. Fit ``points ≈ a + b·ln(adp)`` per position on players that have *both* a
   model projection and a market ADP (global fit as fallback for thin
   positions). The fit is the calibration that puts "the market drafts him at
   pick 12" onto the same points scale as the model.
3. Per player, take the **weighted mean of the available signals** in
   reference-scoring points space (framework §4.1: ``Σ w_s·points_s / Σ w_s``),
   with configurable per-source ``weights``. A player with only one signal
   keeps that signal untouched — nobody is dropped, nothing is invented.
4. Scale the model's component stat line by ``blended / model_points`` so the
   league rescoring path (M4 rescores ``stats``, never trusts ``points``)
   actually *sees* the consensus. ``games`` is left alone — the blend moves
   production, not availability.

**Deliberately not emitted:** market-*only* players (rookies, K/DEF — ADP but
no model stat line). Their ADP signal is already used by the board's own
ADP→VORP gap-fill, which works in league-scored VORP space; emitting a
reference-scaled points guess here would be *worse* calibrated for non-default
leagues and would relabel market guesses as projections. Single-signal rule,
honestly applied: model-only → model record; market-only → the board's ADP
path, exactly as before.

**Off-by-default:** nothing selects this class unless ``PROJECTION_SOURCE=
consensus`` is set (see :func:`~fantasy_coach.ingest.projections.make_projection_source`);
the default remains the single nflverse model, bit-for-bit.

**Caching / degradation:** the blended records cache to JSON per season like
the model's own cache — :meth:`warm_cache` pre-draft, zero network on draft
day. Missing market ADP degrades to the model alone; a dead extra source is
skipped; every degradation logs a warning and stamps the records' ``inputs``
label so you can see exactly what contributed.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from fantasy_coach.ingest.canonical import CanonicalPlayer
from fantasy_coach.ingest.projections import NflverseProjectionSource, default_season
from fantasy_coach.ingest.sources import ProjectionRecord, ProjectionSource

__all__ = [
    "CONSENSUS_NOTE",
    "DEFAULT_BLEND_WEIGHTS",
    "ConsensusProjectionSource",
    "fit_log_points_curve",
    "market_adp_from_players",
]

logger = logging.getLogger(__name__)

#: The honesty label stamped into the consensus cache and the store's note
#: column — a blend of estimates is still an estimate, not a promise.
CONSENSUS_NOTE = (
    "consensus estimate: weighted blend of the nflverse model with market-implied "
    "(ADP-calibrated) points — still a model estimate, labelled per record"
)

#: Default per-source blend weights. The model anchors (it carries the stat
#: line and our own math); the market corrects it where thousands of drafters
#: disagree. Tuned by hand for a first prior — the §4.5 calibration loop is
#: what should learn these eventually.
DEFAULT_BLEND_WEIGHTS: dict[str, float] = {"model": 0.7, "market": 0.3}

#: Weight for an extra (plugged-in) source with no explicit ``weights`` entry.
DEFAULT_EXTRA_WEIGHT = 0.5

#: A position needs this many (model, ADP) pairs for its own calibration
#: curve; thinner positions fall back to the global fit.
_MIN_POSITION_FIT_PAIRS = 3


def market_adp_from_players(players: Iterable[CanonicalPlayer]) -> dict[str, float]:
    """``{gsis_id: adp}`` from canonical players with Yahoo market ADP attached.

    The convenience bridge from the M3/store layer to the blend's ``market``
    input — players without a hub id or an ADP simply don't contribute.
    """
    return {
        p.ids.gsis_id: float(p.market.adp)
        for p in players
        if p.ids.gsis_id and p.market.adp is not None
    }


def fit_log_points_curve(
    pairs: Sequence[tuple[float, float]],
) -> Callable[[float], float] | None:
    """Fit ``points ≈ a + b·ln(adp)`` on (adp, points) pairs — least squares.

    The market's value decay is roughly log-shaped (the board's ADP gap-fill
    uses the same idea in VORP space). Returns ``None`` when the input can't
    support a fit (fewer than two distinct ADPs) — the caller degrades rather
    than pretending to know a curve.
    """
    pts = [(math.log(max(adp, 1.0)), points) for adp, points in pairs]
    if len({x for x, _ in pts}) < 2:
        return None
    n = len(pts)
    mean_x = sum(x for x, _ in pts) / n
    mean_y = sum(y for _, y in pts) / n
    ss_xx = sum((x - mean_x) ** 2 for x, _ in pts)
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in pts)
    b = ss_xy / ss_xx
    a = mean_y - b * mean_x
    return lambda adp: a + b * math.log(max(adp, 1.0))


@dataclass(slots=True)
class ConsensusProjectionSource:
    """Weighted consensus of the model + market (+ pluggable extras).

    Implements the :class:`~fantasy_coach.ingest.sources.ProjectionSource`
    protocol and emits the exact :class:`ProjectionRecord` shape the value
    board consumes — same gsis keying, same ``stats`` dict — so
    ``build_value_board`` needs zero changes.

    Args:
        model: The anchoring stat-line model (default: the free nflverse
            model). Its per-season cache is what makes draft day zero-network.
        market: ``{gsis_id: adp}`` — or a zero-arg callable returning one, so
            callers can bind "read the store's current ADP" lazily. ``None``
            (or empty) degrades the blend to the model alone, with a warning.
        extra_sources: Additional :class:`ProjectionSource` implementations to
            blend (the FantasyPros slot). Only records keyed by ``gsis_id``
            blend in — adapt other id namespaces through the crosswalk first.
            Sources that are not live or that raise are skipped with a warning.
        weights: Per-source blend weights by signal name (``"model"``,
            ``"market"``, or an extra source's ``name``). Renormalized over the
            signals actually present per player, so a missing signal never
            drags the mean toward zero.
        cache_dir: Where the per-season consensus JSON cache lives.
        curve_fitter: The ADP→points calibration fitter — injectable so tests
            can pin an exact curve (production uses :func:`fit_log_points_curve`).
    """

    name: str = "consensus"
    model: ProjectionSource = field(default_factory=NflverseProjectionSource)
    market: Mapping[str, float] | Callable[[], Mapping[str, float]] | None = None
    extra_sources: tuple[ProjectionSource, ...] = ()
    weights: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_BLEND_WEIGHTS)
    )
    cache_dir: Path = field(default_factory=lambda: Path(".cache"))
    curve_fitter: Callable[
        [Sequence[tuple[float, float]]], Callable[[float], float] | None
    ] = field(default=fit_log_points_curve, repr=False)

    @property
    def is_live(self) -> bool:
        """True — the anchor model needs no key (reachability is per-call)."""
        return True

    # -- ProjectionSource protocol -------------------------------------------

    def project(self, *, week: int | None = None, season: int | None = None) -> list[ProjectionRecord]:
        """Consensus projections for ``season`` — cache-first, like the model.

        Raises:
            ValueError: If ``week`` is given — season horizon only, like the model.
            RuntimeError: If no cache exists and the anchor model is unavailable.
        """
        if week is not None:
            raise ValueError(
                "ConsensusProjectionSource blends season-horizon projections only "
                "(week=None), matching the underlying model."
            )
        season = season or default_season()
        cached = self._load_cache(season)
        if cached is not None:
            return cached
        try:
            records = self._compute(season)
        except Exception as exc:
            raise RuntimeError(
                f"consensus blend failed for season {season} and no local cache "
                f"exists at {self._cache_path(season)} — usually the anchor model "
                "is unreachable. Run warm_cache() while online (pre-draft warm "
                "cache, framework §7)."
            ) from exc
        self._write_cache(season, records)
        return records

    def warm_cache(self, season: int | None = None) -> list[ProjectionRecord]:
        """Refresh the model's cache, recompute the blend, persist it (pre-draft).

        The model is warmed first when it knows how (so both caches end up
        fresh); a failed model *warm* still degrades to its own cache via
        ``project()`` inside the blend.
        """
        season = season or default_season()
        model_warm = getattr(self.model, "warm_cache", None)
        if callable(model_warm):
            try:
                model_warm(season)
            except Exception as exc:
                logger.warning(
                    "consensus: model warm_cache failed (%s); blending from the "
                    "model's existing cache if present",
                    exc,
                )
        records = self._compute(season)
        self._write_cache(season, records)
        return records

    # -- the blend ------------------------------------------------------------

    def _market_adp(self) -> Mapping[str, float]:
        """Resolve the market input (mapping or lazy callable) to a mapping."""
        market = self.market() if callable(self.market) else self.market
        return market or {}

    def _extra_points(self, season: int) -> dict[str, dict[str, float]]:
        """``{source_name: {gsis_id: points}}`` from live, healthy extra sources."""
        out: dict[str, dict[str, float]] = {}
        for source in self.extra_sources:
            if not source.is_live:
                logger.warning(
                    "consensus: extra source %r not configured — skipped", source.name
                )
                continue
            try:
                records = source.project(season=season)
            except Exception as exc:
                logger.warning(
                    "consensus: extra source %r failed (%s) — skipped", source.name, exc
                )
                continue
            keyed = {
                r.source_id: r.points for r in records if r.source_id_field == "gsis_id"
            }
            if len(keyed) < len(records):
                logger.warning(
                    "consensus: %d %r records not keyed by gsis_id were ignored "
                    "(resolve them through the crosswalk to blend them)",
                    len(records) - len(keyed),
                    source.name,
                )
            if keyed:
                out[source.name] = keyed
        return out

    def _weight_for(self, signal: str) -> float:
        """The configured weight for a signal (extras default sensibly)."""
        default = DEFAULT_BLEND_WEIGHTS.get(signal, DEFAULT_EXTRA_WEIGHT)
        return float(self.weights.get(signal, default))

    def _compute(self, season: int) -> list[ProjectionRecord]:
        """Blend the model with whatever other signals are actually present."""
        model_records = self.model.project(season=season)
        market = self._market_adp()
        if not market:
            logger.warning(
                "consensus: no market ADP available — blend degrades to the model alone"
            )
        extras = self._extra_points(season)

        # Calibrate ADP → points per position on players with both signals.
        curves: dict[str, Callable[[float], float]] = {}
        if market:
            pairs_by_pos: dict[str, list[tuple[float, float]]] = {}
            all_pairs: list[tuple[float, float]] = []
            for rec in model_records:
                adp = market.get(rec.source_id)
                if adp is None:
                    continue
                pairs_by_pos.setdefault(rec.position, []).append((adp, rec.points))
                all_pairs.append((adp, rec.points))
            global_curve = self.curve_fitter(all_pairs) if all_pairs else None
            for pos, pairs in pairs_by_pos.items():
                curve = (
                    self.curve_fitter(pairs)
                    if len(pairs) >= _MIN_POSITION_FIT_PAIRS
                    else None
                )
                curve = curve or global_curve
                if curve is not None:
                    curves[pos] = curve
            if not curves:
                logger.warning(
                    "consensus: could not calibrate ADP→points (too few overlapping "
                    "players) — blend degrades to the model alone"
                )

        records: list[ProjectionRecord] = []
        for rec in model_records:
            signals: dict[str, float] = {"model": rec.points}
            # The market signal needs a calibration curve for the player's
            # position and a positive model total to scale the stat line by;
            # very-low-value players just keep the model's word.
            adp = market.get(rec.source_id)
            curve = curves.get(rec.position)
            if adp is not None and curve is not None and rec.points > 0:
                signals["market"] = curve(adp)
            for source_name, keyed in extras.items():
                pts = keyed.get(rec.source_id)
                if pts is not None and rec.points > 0:
                    signals[source_name] = pts

            total_weight = sum(self._weight_for(s) for s in signals)
            if len(signals) == 1 or total_weight <= 0:
                blended = rec.points
                inputs = ("model",)
            else:
                blended = (
                    sum(self._weight_for(s) * v for s, v in signals.items())
                    / total_weight
                )
                inputs = tuple(signals)

            stats = dict(rec.stats)
            if len(inputs) > 1 and rec.points > 0 and stats:
                # Scale production components so league rescoring sees the
                # blend; "games" is availability, not production — untouched.
                scale = blended / rec.points
                stats = {
                    key: round(val * scale, 2) if key != "games" else val
                    for key, val in stats.items()
                }
            records.append(
                ProjectionRecord(
                    source=self.name,
                    source_id=rec.source_id,
                    source_id_field="gsis_id",
                    points=round(blended, 2),
                    position=rec.position,
                    team=rec.team,
                    name=rec.name,
                    stats=stats,
                    inputs=inputs,
                )
            )
        records.sort(key=lambda r: r.points, reverse=True)
        return records

    # -- cache (framework §7 "pre-draft warm cache") --------------------------

    def _cache_path(self, season: int) -> Path:
        return self.cache_dir / f"projections_{self.name}_{season}.json"

    def _write_cache(self, season: int, records: list[ProjectionRecord]) -> None:
        """Persist the blend as JSON, stamped with weights + the honesty note."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": self.name,
            "note": CONSENSUS_NOTE,
            "season": season,
            "weights": {k: float(v) for k, v in dict(self.weights).items()},
            "generated_on": date.today().isoformat(),
            "records": [
                {
                    "source_id": r.source_id,
                    "source_id_field": r.source_id_field,
                    "points": r.points,
                    "position": r.position,
                    "team": r.team,
                    "name": r.name,
                    "stats": r.stats,
                    "inputs": list(r.inputs),
                }
                for r in records
            ],
        }
        self._cache_path(season).write_text(json.dumps(payload), encoding="utf-8")

    def _load_cache(self, season: int) -> list[ProjectionRecord] | None:
        """Load a season's cached blend, or ``None`` if absent/unreadable."""
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
                    position=str(r.get("position", "")),
                    team=str(r.get("team", "")),
                    name=str(r.get("name", "")),
                    stats={k: float(v) for k, v in dict(r.get("stats", {})).items()},
                    inputs=tuple(str(i) for i in r.get("inputs", [])),
                )
                for r in payload.get("records", [])
            ]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None  # corrupt cache -> treat as absent, recompute live
