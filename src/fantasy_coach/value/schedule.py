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
    "playoff_weeks",
    "weekly_points",
    "blend_value",
    "schedule_note",
]

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


def blend_value(
    season_vorp: float,
    playoff_vorp: float,
    *,
    weight: float,
    n_playoff_weeks: int,
    n_season_weeks: int,
) -> float:
    """``(1−w)·season_VORP + w·annualized playoff VORP`` — the draft value.

    The playoff VORP is annualized (× season weeks / playoff weeks) so the two
    terms share a scale and the blend is monotonic in ``weight``: turning the
    dial up always moves a player toward their playoff-week strength.
    """
    if n_playoff_weeks <= 0:
        return season_vorp
    annualized = playoff_vorp * (n_season_weeks / n_playoff_weeks)
    return (1.0 - weight) * season_vorp + weight * annualized


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
    if avg >= _SOFT_THRESHOLD:
        return f"soft playoff schedule ({span}: {avg:.2f}× vs {position})"
    if avg <= _TOUGH_THRESHOLD:
        return f"tough playoff schedule ({span}: {avg:.2f}× vs {position})"
    return ""
