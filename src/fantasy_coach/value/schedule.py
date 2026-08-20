"""Schedule-aware valuation: weekly splits, playoff value, the blended draft value.

Step 5 of the framework's draft story (and the M10 seam): the founder's goal is
*"the strongest team over the season, especially the fantasy playoff weeks"* —
so draft value must see the calendar, not just a season total.

The model, in full (no hidden steps — every output is a **model estimate**):

1. **Weekly split.** The projection layer is season-total by design; a weekly
   distribution is derived, not projected: the season total is split evenly
   across the weeks the player's team actually plays (bye excluded), then each
   week is scaled by the opponent-difficulty multiplier from
   :class:`~fantasy_coach.ingest.schedule.SeasonSchedule`. An even split ×
   matchup multiplier is deliberately simple and transparent; it claims
   *direction* (easier week → more expected points), not week-level precision.
2. **Playoff weeks** come from the league's own settings:
   ``playoff_start_week`` plus one week per elimination round
   (``ceil(log2(num_playoff_teams))`` — 6-team playoffs = 3 rounds, weeks
   15–17 in a default Yahoo league). Unset settings fall back to the Yahoo
   defaults, labelled by the caller.
3. **Playoff VORP** = playoff-week points − the replacement baseline prorated
   to those weeks. It answers "how far above a waiver body is this player
   *during your playoffs*", on a per-week-honest scale.
4. **Blended draft value** = ``(1−w)·season_VORP + w·annualized_playoff_VORP``
   where the playoff VORP is annualized (scaled from playoff weeks back to the
   full fantasy season) so both terms live on the same scale and the weight
   ``w`` reads as a true dial: ``w=0`` is exactly the season board (existing
   behavior preserved), ``w=1`` ranks purely by playoff-week strength.

5. **Per-week SOS value** (upgrade 3): the same weekly split summed over the
   *whole* fantasy season is the schedule-adjusted season total —
   ``sos_points = Σ_w weekly_w`` — and ``sos_VORP = sos_points − baseline``.
   Where the playoff VORP looks only at the playoff weeks, this looks at every
   week through its own opponent (position-specific: an RB's Week-9 matchup
   is that week's defense's RB points allowed). The board mixes it into the
   season component with a ``sos_weight`` dial —
   ``season = (1−s)·VORP + s·sos_VORP`` — before the playoff blend, so the
   playoff weeks are still weighted more heavily on top (the two dials
   compose: SOS shades every week, the playoff emphasis re-weights 15–17).
   ``s=0`` keeps the pre-upgrade board exactly.
6. **Weighted SOS score**: :func:`weighted_sos` folds a player's per-week
   profile into one number with playoff weeks counted heavier — the display /
   narration summary of the schedule (1.0 = league-average matchups).

The in-season optimizer (later step) reuses :func:`weekly_points` directly —
rest-of-season value is the same split summed from the current week instead of
week 1, and start/sit is a single week of it.
"""

from __future__ import annotations

import math

from fantasy_coach.clients.models import LeagueSettings
from fantasy_coach.ingest.schedule import SeasonSchedule

__all__ = [
    "DEFAULT_PLAYOFF_START_WEEK",
    "DEFAULT_PLAYOFF_ROUNDS",
    "PLAYOFF_WEEK_WEIGHT",
    "playoff_weeks",
    "weekly_points",
    "blend_value",
    "sos_blend",
    "weighted_sos",
    "extreme_playoff_week",
    "schedule_note",
]

#: How much heavier a playoff week counts than a regular week in the
#: :func:`weighted_sos` summary score (2× — the championship weeks are the
#: ones you can't afford to lose to a bad matchup).
PLAYOFF_WEEK_WEIGHT = 2.0

#: A single playoff week whose multiplier sits this far from neutral gets
#: called out by name in the note (a 0.80× championship-week matchup is worth
#: knowing even when the three-week average is unremarkable).
_EXTREME_WEEK_DELTA = 0.15

#: Yahoo-default fantasy playoff shape, used only when the league's settings
#: don't carry the real values (offline-built settings): weeks 15–17.
DEFAULT_PLAYOFF_START_WEEK = 15
DEFAULT_PLAYOFF_ROUNDS = 3

#: Playoff weeks never extend past the NFL regular season (fantasy leagues
#: finish by week 18 — championship in week 17 for nearly all of them).
_LAST_NFL_WEEK = 18

#: ``schedule_note`` thresholds: average playoff multiplier at/above the first
#: reads "soft" (favorable), at/below the second "tough". Between is unlabelled
#: — a ±5% swing is inside this model's noise and shouldn't be narrated.
_SOFT_THRESHOLD = 1.05
_TOUGH_THRESHOLD = 0.95


def playoff_weeks(settings: LeagueSettings) -> list[int]:
    """The league's fantasy-playoff weeks, from its own settings.

    One week per elimination round: ``ceil(log2(num_playoff_teams))`` rounds
    (byes in a 6-team bracket don't add weeks — 6 teams still resolve in 3).
    Missing settings fall back to the Yahoo defaults (weeks 15–17).
    """
    start = settings.playoff_start_week or DEFAULT_PLAYOFF_START_WEEK
    teams = settings.num_playoff_teams
    if teams is not None and teams >= 2:
        rounds = max(1, math.ceil(math.log2(teams)))
    else:
        rounds = DEFAULT_PLAYOFF_ROUNDS
    return [start + i for i in range(rounds) if start + i <= _LAST_NFL_WEEK]


def weekly_points(
    season_points: float,
    team: str,
    position: str,
    schedule: SeasonSchedule,
    *,
    through_week: int,
) -> dict[int, float]:
    """Split a season projection into matchup-adjusted weekly points.

    Even split across the team's game weeks in ``1..through_week`` (bye weeks
    simply have no entry), each scaled by the opponent-difficulty multiplier.
    Returns ``{}`` for a team the schedule doesn't know — callers must treat
    that as "no weekly view", never as zero production.
    """
    weeks = schedule.game_weeks(team, through=through_week)
    if not weeks:
        return {}
    per_week = season_points / len(weeks)
    return {
        w: per_week * schedule.multiplier(position, schedule.opponent(team, w))
        for w in weeks
    }


#: The annualized playoff term may pull a player's draft value at most this
#: many season-scale points away from their season VORP (before the ``weight``
#: dial scales it down further). Two-to-three playoff weeks annualized ×5 is
#: single-season matchup noise amplified — uncapped it could swing a player
#: across whole tiers on the strength of one soft week-16 opponent.
PLAYOFF_SWING_CAP = 40.0


def blend_value(
    season_vorp: float,
    playoff_vorp: float,
    *,
    weight: float,
    n_playoff_weeks: int,
    n_season_weeks: int,
    swing_cap: float = PLAYOFF_SWING_CAP,
) -> float:
    """``season_VORP + w·clamp(annualized playoff VORP − season_VORP)``.

    The playoff VORP is annualized (× season weeks / playoff weeks) so the two
    terms share a scale and the blend is monotonic in ``weight``: turning the
    dial up always moves a player toward their playoff-week strength. The
    annualized deviation is capped at ``±swing_cap`` season-points so
    single-season schedule noise can nudge the ranking but never rewrite a
    tier (algebraically identical to the plain ``(1−w)·s + w·a`` blend inside
    the cap).
    """
    if n_playoff_weeks <= 0:
        return season_vorp
    annualized = playoff_vorp * (n_season_weeks / n_playoff_weeks)
    delta = max(-swing_cap, min(swing_cap, annualized - season_vorp))
    return season_vorp + weight * delta


def sos_blend(season_vorp: float, sos_vorp: float, *, weight: float) -> float:
    """``(1−s)·season_VORP + s·per-week-SOS VORP`` — the season component.

    Identity at ``weight=0`` (the pre-upgrade board); at ``weight=1`` every
    week of the season is valued through its own matchup.
    """
    return (1.0 - weight) * season_vorp + weight * sos_vorp


def weighted_sos(
    week_multipliers: dict[int, float],
    playoff_weeks: list[int],
    *,
    playoff_weight: float = PLAYOFF_WEEK_WEIGHT,
) -> float | None:
    """One-number schedule summary: the per-week multipliers averaged with
    playoff weeks counted ``playoff_weight``× heavier. ``None`` with no weeks.
    """
    if not week_multipliers:
        return None
    pw = set(playoff_weeks)
    total = 0.0
    weight_sum = 0.0
    for week, mult in week_multipliers.items():
        w = playoff_weight if week in pw else 1.0
        total += w * mult
        weight_sum += w
    return total / weight_sum if weight_sum else None


def extreme_playoff_week(
    position: str, team: str, schedule: SeasonSchedule, weeks: list[int]
) -> tuple[int, str, float] | None:
    """The playoff week furthest from neutral, if it clears the call-out bar.

    Returns ``(week, opponent, multiplier)`` or ``None``.
    """
    best: tuple[int, str, float] | None = None
    for w in weeks:
        opp = schedule.opponent(team, w)
        if opp is None:
            continue
        mult = schedule.multiplier(position, opp)
        if abs(mult - 1.0) < _EXTREME_WEEK_DELTA:
            continue
        if best is None or abs(mult - 1.0) > abs(best[2] - 1.0):
            best = (w, opp, mult)
    return best


def schedule_note(
    position: str,
    team: str,
    schedule: SeasonSchedule,
    weeks: list[int],
) -> str:
    """A short, honest narration of a player's playoff-week schedule.

    Only speaks when there is something to say: a bye landing *inside* the
    playoff weeks (rare but roster-critical), or an average playoff multiplier
    outside the ``soft``/``tough`` thresholds. Neutral schedules stay silent
    rather than narrating noise.
    """
    if not weeks or not team:
        return ""
    span = f"wk{weeks[0]}–{weeks[-1]}" if len(weeks) > 1 else f"wk{weeks[0]}"
    missed = [w for w in weeks if schedule.opponent(team, w) is None]
    if missed and schedule.game_weeks(team, through=weeks[-1]):
        return f"no game wk{missed[0]} — misses a playoff week"
    mults = [
        schedule.multiplier(position, schedule.opponent(team, w)) for w in weeks
    ]
    if not mults:
        return ""
    avg = sum(mults) / len(mults)
    extreme = extreme_playoff_week(position, team, schedule, weeks)
    detail = ""
    if extreme is not None:
        week, opp, mult = extreme
        detail = f"; wk{week} vs {opp} {mult:.2f}×"
    if avg >= _SOFT_THRESHOLD:
        return f"soft playoff schedule ({span}: {avg:.2f}× vs {position}{detail})"
    if avg <= _TOUGH_THRESHOLD:
        return f"tough playoff schedule ({span}: {avg:.2f}× vs {position}{detail})"
    if extreme is not None:
        week, opp, mult = extreme
        kind = "soft" if mult > 1.0 else "tough"
        return f"{kind} wk{week} matchup vs {opp} ({mult:.2f}× vs {position})"
    return ""
