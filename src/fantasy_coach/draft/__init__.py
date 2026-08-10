"""M5 — the live draft companion (framework §4.3, §7; the Sept deliverable).

Polls Yahoo ``draftresults``, rebuilds the drafted set every cycle (undo-safe),
recomputes the league's available value board with baselines that shift as the
pools drain, weights it by the founder's unfilled roster slots, and serves the
whole thing as a dark auto-refreshing local page to sit next to the Yahoo
draft room. ``--simulate`` replays a scripted draft through the identical loop
so everything is testable long before draft night.
"""

from fantasy_coach.draft.loop import (
    DraftLoop,
    PickSource,
    StatusSource,
    YahooPickSource,
)
from fantasy_coach.draft.recommend import (
    Recommendation,
    RosterNeeds,
    assign_roster,
    build_recommendation,
    compute_needs,
    rank_available,
    roster_slots,
)
from fantasy_coach.draft.simulate import SimulatedPickSource, script_draft, sim_team_names
from fantasy_coach.draft.state import DraftState, ResolvedPick, snake_team_for_pick
from fantasy_coach.draft.web import CompanionServer

__all__ = [
    "DraftLoop",
    "PickSource",
    "StatusSource",
    "YahooPickSource",
    "Recommendation",
    "RosterNeeds",
    "assign_roster",
    "build_recommendation",
    "compute_needs",
    "rank_available",
    "roster_slots",
    "SimulatedPickSource",
    "script_draft",
    "sim_team_names",
    "DraftState",
    "ResolvedPick",
    "snake_team_for_pick",
    "CompanionServer",
]
