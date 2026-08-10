"""M4 — the projection/value engine (framework §4.1–4.2, + step-5 schedule).

Turns stat-line projections + a league's exact :class:`LeagueSettings` into a
ranked, cross-position VORP board: league-rescored points, roster-demand-derived
replacement baselines (FLEX/superflex aware), gap-based tiers, and ADP/flat
gap-fill for players the projection model can't see (rookies, K, DEF). With a
:class:`~fantasy_coach.ingest.schedule.SeasonSchedule` attached, the board also
carries matchup-adjusted playoff VORP and a season↔playoff blended draft value
(:mod:`fantasy_coach.value.schedule`).
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
from fantasy_coach.value.injury import (
    STATUS_DISCOUNTS,
    PlayerRisk,
    build_risk_index,
    injury_multiplier,
    injury_note,
    total_discount,
)
from fantasy_coach.value.schedule import (
    blend_value,
    playoff_weeks,
    schedule_note,
    weekly_points,
)
from fantasy_coach.value.scoring import (
    YAHOO_STAT_KEYS,
    league_points,
    league_scoring,
    score_stats,
)

__all__ = [
    "STATUS_DISCOUNTS",
    "PlayerRisk",
    "build_risk_index",
    "total_discount",
    "injury_multiplier",
    "injury_note",
    "playoff_weeks",
    "weekly_points",
    "blend_value",
    "schedule_note",
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
