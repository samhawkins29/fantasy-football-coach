"""Projection *distributions*: floor / median / ceiling from week-to-week variance.

Framework §4.1 step 5 — "carry a projection distribution (mean + variance /
floor–ceiling), not just a point estimate. Estimate variance from source
disagreement + role volatility." This module is the free, offline half of
that: it turns a player's **historical week-to-week fantasy scoring variance**
(nflverse weekly box scores, already pulled by the projection model) into a
season-total spread, and hands back a floor and a ceiling around the median
projection. The consensus source adds the *source-disagreement* half on top
(:func:`widen_for_disagreement`).

The model, in full (no hidden steps — every output is a **model estimate**):

1. **Weekly volatility.** For every player, the coefficient of variation
   (σ/mean) of their weekly reference-scored fantasy points across the history
   window. Weeks with a stat row count; a game with 0 points still counts (a
   dud *is* variance). Players with fewer than :data:`MIN_WEEKS_FOR_OWN_CV`
   scored weeks lean on the positional prior.
2. **Positional prior.** The mean weekly CV of qualified players at the
   position (fallback :data:`POSITION_CV_PRIOR` when the pull is too thin —
   RBs/TEs are boom-bust, QBs steadier). Each player's CV is shrunk toward it:
   ``cv = (n·own + K·prior) / (n + K)``.
3. **Season spread from weekly noise.** Weekly points are treated as roughly
   independent draws, so the season total's noise scales with ``√games``:
   ``σ_weekly_total = mean_weekly · cv · √G`` — i.e. relative spread shrinks
   with more games (a 17-game season is far more predictable than one week).
4. **Role / sample uncertainty.** A projection built mostly from a positional
   prior (few historical games, new role) is *far* less certain than one built
   on 40 games of evidence. That is modelled as a second, additive relative
   spread ``role_cv = ROLE_CV_BASE + ROLE_CV_SHRINK · shrink_weight`` where
   ``shrink_weight = K/(G+K)`` is exactly how much of the point projection came
   from the prior. ``ROLE_CV_BASE`` is the irreducible season-projection error
   nobody escapes (coaching, usage, injuries not otherwise modelled).
5. **Combine** the two independent components in quadrature into one relative
   spread ``rel_sigma``; ``floor = points · (1 − z·rel_sigma)`` (clipped at 0)
   and ``ceiling = points · (1 + z·rel_sigma)`` with ``z`` = the
   :data:`FLOOR_CEILING_Z` normal quantile (~20th / 80th percentile).

Everything downstream consumes the spread as **ratios** to the point
projection (:func:`spread_ratios`) so it survives league rescoring unchanged:
the value board multiplies the league-scored points by ``floor_ratio`` /
``ceiling_ratio``, keeping floor ≤ median ≤ ceiling in *any* scoring system.

Honest limits: this claims *relative* uncertainty (who is riskier than whom,
and by roughly how much), not calibrated percentiles — the calibration loop
(framework §4.5) is what checks that later.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "VARIANCE_NOTE",
    "POSITION_CV_PRIOR",
    "FLOOR_CEILING_Z",
    "MIN_WEEKS_FOR_OWN_CV",
    "SpreadModel",
    "SpreadEstimate",
    "weekly_cv",
    "positional_cv_priors",
    "spread_ratios",
    "widen_for_disagreement",
]

#: The honesty label for every floor/ceiling this module produces.
VARIANCE_NOTE = (
    "model estimate (nflverse-based): floor/ceiling from historical week-to-week "
    "scoring variance shrunk to positional priors, plus role/sample uncertainty"
)

#: Fallback weekly coefficient of variation by position, from the well-known
#: shape of fantasy scoring: QBs are the steadiest, TEs/RBs the most boom-bust.
#: Only used when the history pull can't produce a qualified prior itself.
POSITION_CV_PRIOR: dict[str, float] = {
    "QB": 0.45,
    "RB": 0.65,
    "WR": 0.70,
    "TE": 0.80,
    "DL": 0.75,  # sack-driven: boom/bust
    "LB": 0.50,  # tackle volume: the steadiest IDP group
    "DB": 0.65,
}
_DEFAULT_CV_PRIOR = 0.65

#: Normal quantile for the floor/ceiling: 0.84 ≈ 20th / 80th percentile — wide
#: enough to mean something, tight enough to be *a* number rather than a range
#: of everything.
FLOOR_CEILING_Z = 0.84

#: Scored weeks a player needs before their own CV counts at all.
MIN_WEEKS_FOR_OWN_CV = 4


@dataclass(slots=True)
class SpreadModel:
    """The knobs of the spread model (all documented in the module docstring).

    Args:
        shrink_weeks: ``K`` for shrinking a player's own weekly CV toward the
            positional prior (weeks of evidence that outweigh the prior).
        role_cv_base: Irreducible relative season-projection uncertainty.
        role_cv_shrink: Extra relative uncertainty for a projection built
            entirely from the positional prior (scaled by the shrink weight).
        z: Normal quantile for floor/ceiling.
        max_rel_sigma: Cap on the relative spread (keeps a 2-game sample from
            producing a ceiling three times the median).
    """

    shrink_weeks: float = 8.0
    role_cv_base: float = 0.15
    role_cv_shrink: float = 0.35
    z: float = FLOOR_CEILING_Z
    max_rel_sigma: float = 0.60


@dataclass(slots=True)
class SpreadEstimate:
    """One player's spread: the shrunk weekly CV and the season-relative σ."""

    weekly_cv: float
    rel_sigma: float
    floor_ratio: float
    ceiling_ratio: float
    weeks_seen: int


def weekly_cv(points: Sequence[float]) -> float | None:
    """Coefficient of variation (population σ / mean) of a weekly points list.

    Returns ``None`` when there are fewer than two weeks or the mean is not
    positive (nothing to be relative to).
    """
    if len(points) < 2:
        return None
    n = len(points)
    mean = sum(points) / n
    if mean <= 0:
        return None
    var = sum((p - mean) ** 2 for p in points) / n
    return math.sqrt(var) / mean


def positional_cv_priors(
    weekly_by_player: Mapping[str, Sequence[float]],
    positions: Mapping[str, str],
    *,
    min_weeks: int = 8,
) -> dict[str, float]:
    """Mean weekly CV of qualified players per position (≥ ``min_weeks`` weeks).

    Positions with no qualified player fall back to :data:`POSITION_CV_PRIOR`.
    """
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for pid, pts in weekly_by_player.items():
        if len(pts) < min_weeks:
            continue
        cv = weekly_cv(pts)
        if cv is None:
            continue
        pos = positions.get(pid, "")
        sums[pos] = sums.get(pos, 0.0) + cv
        counts[pos] = counts.get(pos, 0) + 1
    priors = dict(POSITION_CV_PRIOR)
    for pos, total in sums.items():
        priors[pos] = total / counts[pos]
    return priors


def spread_ratios(
    weekly_points: Sequence[float],
    *,
    position: str,
    proj_games: float,
    shrink_weight: float,
    prior_cv: float | None = None,
    model: SpreadModel | None = None,
) -> SpreadEstimate:
    """Floor/ceiling ratios for one player (see the module docstring's steps 1–5).

    Args:
        weekly_points: The player's historical weekly reference-scored points.
        position: Normalized position (picks the CV prior fallback).
        proj_games: Games the point projection assumes (the √G in step 3).
        shrink_weight: ``K/(G+K)`` from the point model — the share of the
            projection that came from the positional prior (0 = all evidence,
            1 = all prior).
        prior_cv: The positional weekly-CV prior; defaults to
            :data:`POSITION_CV_PRIOR`.
        model: The knobs; defaults to :class:`SpreadModel`'s defaults.
    """
    m = model or SpreadModel()
    prior = prior_cv if prior_cv is not None else POSITION_CV_PRIOR.get(position, _DEFAULT_CV_PRIOR)
    n = len(weekly_points)
    own = weekly_cv(weekly_points) if n >= MIN_WEEKS_FOR_OWN_CV else None
    if own is None:
        cv = prior
    else:
        cv = (n * own + m.shrink_weeks * prior) / (n + m.shrink_weeks)

    games = max(1.0, float(proj_games))
    # Step 3: weekly noise on the season total, relative to the total —
    # (mean·cv·√G) / (mean·G) = cv/√G.
    weekly_component = cv / math.sqrt(games)
    # Step 4: role/sample uncertainty.
    shrink = min(1.0, max(0.0, float(shrink_weight)))
    role_component = m.role_cv_base + m.role_cv_shrink * shrink
    rel_sigma = min(m.max_rel_sigma, math.hypot(weekly_component, role_component))

    floor_ratio = max(0.0, 1.0 - m.z * rel_sigma)
    ceiling_ratio = 1.0 + m.z * rel_sigma
    return SpreadEstimate(
        weekly_cv=round(cv, 4),
        rel_sigma=round(rel_sigma, 4),
        floor_ratio=round(floor_ratio, 4),
        ceiling_ratio=round(ceiling_ratio, 4),
        weeks_seen=n,
    )


def widen_for_disagreement(
    points: float,
    floor: float | None,
    ceiling: float | None,
    signals: Iterable[float],
    *,
    z: float = FLOOR_CEILING_Z,
) -> tuple[float | None, float | None]:
    """Widen a floor/ceiling for disagreement between projection sources.

    The framework's other variance input (§4.1 step 5): when independent
    signals (model, market-implied, a paid source…) disagree, the true
    uncertainty is at least their spread. The signals' population σ is added
    in quadrature to the existing spread (``(ceiling − points)/z`` treated as
    the current σ), symmetrically. With one signal or no existing spread the
    inputs pass through unchanged; a missing floor/ceiling stays missing.
    """
    vals = [float(v) for v in signals]
    if points <= 0 or floor is None or ceiling is None or len(vals) < 2:
        return floor, ceiling
    mean = sum(vals) / len(vals)
    dis_sigma = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    if dis_sigma <= 0:
        return floor, ceiling
    cur_sigma = max(0.0, (ceiling - points) / z)
    sigma = math.hypot(cur_sigma, dis_sigma)
    return max(0.0, points - z * sigma), points + z * sigma
