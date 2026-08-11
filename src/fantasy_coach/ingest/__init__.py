"""Ingest subpackage — M3: the shared data layer (framework §3, §2.5).

This is the layer that turns "a Yahoo league" into "every player, joined to all
the data". It has three parts:

* **A canonical player model** (:mod:`~fantasy_coach.ingest.canonical`) keyed by
  the hub id ``gsis_id`` (synthetic ids for defenses / unresolved players), with
  slots the value engine (M4) fills — projections, opportunity, environment,
  market, value.
* **The id crosswalk + resolver** (:mod:`~fantasy_coach.ingest.crosswalk`,
  :mod:`~fantasy_coach.ingest.resolver`) — the critical seam that maps a Yahoo
  ``player_id`` onto the canonical id via the framework's hub-and-spoke pipeline
  (§3.2): direct id → deterministic ``(name, pos, team)`` → rapidfuzz fallback →
  manual overrides → team-defense mapping, reporting coverage + a review queue.
* **External feeds behind one interface** (:mod:`~fantasy_coach.ingest.sources`)
  — nflverse + Sleeper live (free, no key), FantasyPros/Odds API/Open-Meteo
  wired but key-gated, all implementing the ``DataSource`` / ``ProjectionSource``
  protocols so M4/M11 can blend and reweight them.
* **A concrete free projection model** (:mod:`~fantasy_coach.ingest.projections`)
  — :class:`~fantasy_coach.ingest.projections.NflverseProjectionSource` derives
  season stat-line projections from nflverse history (labelled "model estimate"),
  cached locally so draft day needs zero network;
  :func:`~fantasy_coach.ingest.projections.make_projection_source` picks the
  configured source (free nflverse by default, FantasyPros when keyed).
* **An opt-in consensus blend** (:mod:`~fantasy_coach.ingest.consensus`) —
  :class:`~fantasy_coach.ingest.consensus.ConsensusProjectionSource` blends the
  model with market-implied (ADP-calibrated) points behind the same protocol;
  ``PROJECTION_SOURCE=consensus`` turns it on, the default stays single-source.

:func:`~fantasy_coach.ingest.index.build_player_index` assembles it all into a
:class:`~fantasy_coach.ingest.index.PlayerIndex` — the object M4 consumes.

Everything here is **offline-testable** (framework requirement carried from
M1/M2): ``nfl_data_py`` is imported lazily and its fetchers are injectable;
Sleeper is built on an injectable ``httpx.Client``; nothing touches the network
at import time or in the test suite.

Typical use (after M2 has pulled the league's players)::

    from fantasy_coach.ingest import load_id_crosswalk, IdResolver, build_player_index

    crosswalk = load_id_crosswalk()                     # live: nfl_data_py.import_ids()
    resolver = IdResolver(crosswalk, overrides=my_overrides)
    index = build_player_index((p.identity() for p in players), resolver)
    print(index.report.summary())                       # coverage + review queue
"""

from __future__ import annotations

from fantasy_coach.ingest.canonical import (
    CanonicalPlayer,
    ExternalIds,
    GameEnvironment,
    MarketSignals,
    Opportunity,
    PlayerValue,
    Projections,
    dst_canonical_id,
    unresolved_canonical_id,
)
from fantasy_coach.ingest.consensus import (
    CONSENSUS_NOTE,
    ConsensusProjectionSource,
    market_adp_from_players,
)
from fantasy_coach.ingest.crosswalk import (
    CrosswalkRow,
    IdCrosswalk,
    clean_source_id,
    load_id_crosswalk,
)
from fantasy_coach.ingest.index import PlayerIndex, build_player_index
from fantasy_coach.ingest.injury import (
    DURABILITY_NOTE,
    DurabilityProfile,
    DurabilitySource,
    InjuryReport,
    SleeperStatusSource,
    merge_reports,
    normalize_status,
)
from fantasy_coach.ingest.projections import (
    PROJECTED_STAT_KEYS,
    PROJECTION_NOTE,
    REFERENCE_SCORING,
    NflverseProjectionSource,
    default_season,
    make_projection_source,
    score_stats,
)
from fantasy_coach.ingest.names import (
    CANONICAL_TEAMS,
    clean_name,
    is_defense_position,
    match_key,
    normalize_position,
    normalize_team,
)
from fantasy_coach.ingest.schedule import (
    SCHEDULE_NOTE,
    ScheduleSource,
    SeasonSchedule,
)
from fantasy_coach.ingest.resolver import (
    DEFAULT_FUZZY_THRESHOLD,
    IdResolver,
    Resolution,
    ResolutionReport,
    load_overrides,
)
from fantasy_coach.ingest.sources import (
    DataSource,
    FantasyProsSource,
    ImpliedTotal,
    NflverseSource,
    OddsApiSource,
    OpenMeteoSource,
    ProjectionRecord,
    ProjectionSource,
    SleeperSource,
    SourceNotConfigured,
    TrendingPlayer,
    WeatherReport,
)

__all__ = [
    # canonical model
    "CanonicalPlayer",
    "ExternalIds",
    "Projections",
    "Opportunity",
    "GameEnvironment",
    "MarketSignals",
    "PlayerValue",
    "dst_canonical_id",
    "unresolved_canonical_id",
    # names
    "clean_name",
    "normalize_team",
    "normalize_position",
    "is_defense_position",
    "match_key",
    "CANONICAL_TEAMS",
    # crosswalk
    "IdCrosswalk",
    "CrosswalkRow",
    "load_id_crosswalk",
    "clean_source_id",
    # resolver
    "IdResolver",
    "Resolution",
    "ResolutionReport",
    "DEFAULT_FUZZY_THRESHOLD",
    "load_overrides",
    # sources
    "DataSource",
    "ProjectionSource",
    "SourceNotConfigured",
    "NflverseSource",
    "SleeperSource",
    "FantasyProsSource",
    "OddsApiSource",
    "OpenMeteoSource",
    "ProjectionRecord",
    "TrendingPlayer",
    "ImpliedTotal",
    "WeatherReport",
    # projections (the free ProjectionSource — M4's default input)
    "NflverseProjectionSource",
    "make_projection_source",
    "score_stats",
    "default_season",
    "PROJECTED_STAT_KEYS",
    "REFERENCE_SCORING",
    "PROJECTION_NOTE",
    # consensus blend (enhancement 1 — opt-in via PROJECTION_SOURCE=consensus)
    "ConsensusProjectionSource",
    "market_adp_from_players",
    "CONSENSUS_NOTE",
    # schedule (step 5 — opponent map, byes, matchup difficulty)
    "SeasonSchedule",
    "ScheduleSource",
    "SCHEDULE_NOTE",
    # injury/durability (step 6 — current status + re-injury risk signal)
    "InjuryReport",
    "normalize_status",
    "merge_reports",
    "SleeperStatusSource",
    "DurabilityProfile",
    "DurabilitySource",
    "DURABILITY_NOTE",
    # index
    "PlayerIndex",
    "build_player_index",
]
