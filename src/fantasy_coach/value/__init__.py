"""M4 — the projection/value engine (framework §4.1–4.2).

Turns stat-line projections + a league's exact :class:`LeagueSettings` into a
ranked, cross-position VORP board: league-rescored points, roster-demand-derived
replacement baselines (FLEX/superflex aware), gap-based tiers, and ADP/flat
gap-fill for players the projection model can't see (rookies, K, DEF).
"""

from fantasy_coach.value.board import (
    SOURCE_ADP,
    SOURCE_FLAT,
    SOURCE_PROJECTION,
    BoardEntry,
    ValueBoard,
    assign_tiers,
    build_value_board,
    replacement_baselines,
    starter_demand,
)
from fantasy_coach.value.scoring import (
    YAHOO_STAT_KEYS,
    league_points,
    league_scoring,
    score_stats,
)

__all__ = [
    "SOURCE_PROJECTION",
    "SOURCE_ADP",
    "SOURCE_FLAT",
    "BoardEntry",
    "ValueBoard",
    "assign_tiers",
    "build_value_board",
    "replacement_baselines",
    "starter_demand",
    "YAHOO_STAT_KEYS",
    "league_points",
    "league_scoring",
    "score_stats",
]
