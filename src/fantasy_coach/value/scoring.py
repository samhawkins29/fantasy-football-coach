"""League-settings → scoring map: rescore projections in *your* league's points.

M4 §4.1 step 1: never trust a source's pre-scored total when the raw stat line
is available — convert every projection's component stats through the league's
own ``stat_modifiers`` so PPR type, passing-TD value, turnover penalties, and
any custom per-stat tweak all flow from the actual :class:`LeagueSettings`.
Nothing here hardcodes a scoring system; the only hardcoded thing is the
*dictionary* between Yahoo's stat ids and the component-stat keys the
projection layer emits (:data:`~fantasy_coach.ingest.projections.PROJECTED_STAT_KEYS`).

Two managers in different leagues get different point totals from the same stat
line — that is the point (framework §3.4).
"""

from __future__ import annotations

from collections.abc import Mapping

from fantasy_coach.clients.models import LeagueSettings
from fantasy_coach.ingest.projections import REFERENCE_SCORING, score_stats

__all__ = ["YAHOO_STAT_KEYS", "league_scoring", "league_points", "score_stats"]

#: Yahoo NFL stat id → the projection stat key it scores. This is the join
#: between a league's ``stat_modifiers`` (keyed by Yahoo's numeric stat ids —
#: see ``models.STAT_ID_RECEPTIONS``/``STAT_ID_PASSING_TDS``) and the component
#: stat line every :class:`~fantasy_coach.ingest.sources.ProjectionRecord`
#: carries. League stats with no projected component (return yards/TDs, offensive
#: fumble-return TDs…) are skipped: they can't be rescored from a projection
#: that doesn't project them, and they're near-zero EV for skill players anyway.
#: The IDP block (79–88) is Yahoo's individual-defensive-player categories.
YAHOO_STAT_KEYS: dict[int, str] = {
    4: "pass_yds",  # Passing Yards
    5: "pass_td",  # Passing Touchdowns (4-pt vs 6-pt leagues live here)
    6: "pass_int",  # Interceptions thrown
    9: "rush_yds",  # Rushing Yards
    10: "rush_td",  # Rushing Touchdowns
    11: "rec",  # Receptions (PPR type lives here)
    12: "rec_yds",  # Receiving Yards
    13: "rec_td",  # Receiving Touchdowns
    16: "two_pt",  # 2-Point Conversions
    18: "fum_lost",  # Fumbles Lost
    79: "tackle_solo",  # Tackle Solo (IDP)
    80: "tackle_ast",  # Tackle Assist (IDP)
    81: "sack",  # Sack (IDP)
    82: "def_int",  # Interception (IDP)
    83: "ff",  # Fumble Force (IDP)
    84: "fr",  # Fumble Recovery (IDP)
    85: "def_td",  # Defensive Touchdown (IDP)
    86: "safety",  # Safety (IDP)
    87: "pass_def",  # Pass Defended (IDP)
    88: "blk",  # Block Kick (IDP)
}


def league_scoring(settings: LeagueSettings) -> dict[str, float]:
    """Build ``{stat_key: points_per_unit}`` from the league's own stat modifiers.

    Every scored Yahoo stat that maps onto a projected component
    (:data:`YAHOO_STAT_KEYS`) is carried through at the league's exact per-unit
    value — so full/half/zero PPR, 6-pt passing TDs, TE-premium-style reception
    tweaks, and custom turnover penalties all come from the settings object,
    never from a constant here.

    A settings object with **no** scored stats (e.g. constructed before the
    ``/settings`` pull) falls back to :data:`REFERENCE_SCORING` (half-PPR,
    Yahoo-default-shaped) so callers still get a sane board — but a real league
    always produces its own map.
    """
    modifiers = settings.stat_modifiers()
    scoring = {
        key: float(modifiers[stat_id])
        for stat_id, key in YAHOO_STAT_KEYS.items()
        if stat_id in modifiers
    }
    return scoring if scoring else dict(REFERENCE_SCORING)


def league_points(stats: Mapping[str, float], settings: LeagueSettings) -> float:
    """Score one projected stat line in this league's points (convenience)."""
    return score_stats(stats, league_scoring(settings))
