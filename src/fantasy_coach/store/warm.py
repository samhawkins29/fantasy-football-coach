"""``warm_store`` — populate the store for a league in one call (framework §7).

The pre-draft warm cache, made durable: pull/settle every static input (league
settings, canonical players, ADP, projections, stats history), compute the
league's value board via :func:`~fantasy_coach.value.board.build_value_board`,
and persist all of it into the queryable SQLite store. Run it once online
before the draft; on draft day everything is served from the file with zero
network.

The call takes *data objects*, not live clients — the caller composes the live
pulls (or doesn't, offline) and hands in whatever it has::

    from fantasy_coach.ingest.projections import NflverseProjectionSource
    from fantasy_coach.store import CoachStore, warm_store

    store = CoachStore()
    settings = yahoo.get_league_settings(league_key)      # or built offline
    index = build_player_index(identities, resolver)      # M3 crosswalk
    index.attach_yahoo_market(market)                     # Yahoo ADP → players
    result = warm_store(
        store, settings,
        projection_source=NflverseProjectionSource(),     # serves warm cache
        players=index.players.values(),
        stats_rows=nflverse.weekly_stats([2024, 2025]),   # optional history
    )
    print(result.summary())

**Offline degradation is the design center**, not an afterthought — every
input is optional and every failure downgrades to a warning while prior rows
survive:

* ``projections``: served from the step-1 JSON cache with zero network; if
  even that fails, the board is rebuilt from the *store's* projection rows.
* ``players`` absent (no Yahoo session): identity rows are synthesized from
  projection metadata and inserted fill-only, never overwriting richer
  crosswalked rows from an earlier online warm.
* ADP absent: prior ``adp`` rows are kept, and the board build re-attaches
  them from the store — so the ADP→VORP gap-fill keeps working offline. This
  is exactly why ADP is persisted rather than treated as ephemeral.
* ``stats_rows`` absent: prior history rows are kept.

Re-running is always safe: every table upserts (the board snapshot replaces),
and ``data_vintage`` records when each slice was last actually refreshed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from fantasy_coach.clients.models import LeagueSettings
from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
from fantasy_coach.ingest.names import normalize_position, normalize_team
from fantasy_coach.ingest.projections import (
    PROJECTED_STAT_KEYS,
    PROJECTION_NOTE,
    default_season,
)
from fantasy_coach.ingest.schedule import SeasonSchedule
from fantasy_coach.ingest.sources import ProjectionRecord, ProjectionSource
from fantasy_coach.store.store import CoachStore
from fantasy_coach.value.board import build_value_board

__all__ = ["WarmResult", "warm_store", "stats_rows_from_nflverse"]

#: ``resolution_method`` stamped on identity rows synthesized from projection
#: metadata (the offline players fallback) — distinguishable from real
#: crosswalk methods (``yahoo_id``/``deterministic``/``fuzzy``/…) in the DB.
SYNTHESIZED_FROM_PROJECTIONS = "projection_meta"

#: Extra nflverse usage columns carried into ``stats_history`` beyond the
#: projected component stats (volume is the stickiest signal — §4.1 step 3).
_USAGE_COLUMNS: dict[str, tuple[str, ...]] = {
    "carries": ("carries",),
    "targets": ("targets",),
}


@dataclass(slots=True)
class WarmResult:
    """What one warm pass did: refreshed scopes, warnings, resulting counts."""

    league_key: str
    season: int
    counts: dict[str, int] = field(default_factory=dict)
    refreshed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    board_entries: int = 0
    skipped_no_signal: int = 0

    def summary(self) -> str:
        """A printable one-glance report of the warm pass."""
        lines = [
            f"Warmed store for {self.league_key} (season {self.season}):",
            *(f"  {table:>15}: {count} rows" for table, count in self.counts.items()),
            f"  board entries: {self.board_entries}"
            + (f" ({self.skipped_no_signal} skipped, no signal)" if self.skipped_no_signal else ""),
        ]
        if self.refreshed:
            lines.append("  refreshed: " + ", ".join(self.refreshed))
        for warning in self.warnings:
            lines.append(f"  WARNING: {warning}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Row shaping helpers
# --------------------------------------------------------------------------- #


def _records_of(frame: object) -> list[Mapping[str, object]]:
    """Rows from a DataFrame (``to_dict("records")``) or a plain list of dicts."""
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


def stats_rows_from_nflverse(rows: object) -> list[dict[str, object]]:
    """Shape nflverse weekly rows into ``stats_history`` upsert rows.

    Handles both nflverse schema vintages the projections module documents
    (pre-2025 ``recent_team``/``interceptions`` vs 2025 ``team``/
    ``passing_interceptions``) by reusing the same column-coalescing
    :data:`PROJECTED_STAT_KEYS` sums, plus the carry/target usage columns.
    Rows without a ``player_id`` (the gsis hub id) are dropped.
    """
    shaped: list[dict[str, object]] = []
    for row in _records_of(rows):
        gsis_id = str(row.get("player_id", "") or "")
        if not gsis_id:
            continue
        team = str(row.get("recent_team") or row.get("team") or "")
        out: dict[str, object] = {
            "canonical_id": gsis_id,
            "season": int(_num(row, "season")),
            "week": int(_num(row, "week")),
            "season_type": str(row.get("season_type", "REG") or "REG"),
            "position": normalize_position(str(row.get("position", "") or "")),
            "team": normalize_team(team) or "",
        }
        for key, columns in {**PROJECTED_STAT_KEYS, **_USAGE_COLUMNS}.items():
            out[key] = sum(_num(row, c) for c in columns)
        shaped.append(out)
    return shaped


def _players_from_projections(records: Sequence[ProjectionRecord]) -> list[CanonicalPlayer]:
    """Minimal identity rows from projection metadata (the offline fallback).

    Enough to make the ``players`` table queryable without a Yahoo session:
    hub id, name, position, team. No yahoo id, bye, or status — which is why
    these rows are inserted fill-only.
    """
    return [
        CanonicalPlayer(
            canonical_id=rec.source_id,
            ids=ExternalIds(gsis_id=rec.source_id),
            name=rec.name,
            position=rec.position,
            team=rec.team,
            resolution_method=SYNTHESIZED_FROM_PROJECTIONS,
        )
        for rec in records
        if rec.source_id
    ]


def _adp_rows_from_players(players: Iterable[CanonicalPlayer]) -> list[dict[str, object]]:
    """ADP upsert rows from players whose ``market.adp`` was attached (M3 join)."""
    return [
        {
            "canonical_id": p.canonical_id,
            "average_pick": p.market.adp,
            "stdev": p.market.adp_stddev,
        }
        for p in players
        if p.market.adp is not None
    ]


# --------------------------------------------------------------------------- #
# The warm pass
# --------------------------------------------------------------------------- #


def warm_store(
    store: CoachStore,
    settings: LeagueSettings,
    *,
    projection_source: ProjectionSource | None = None,
    projections: Sequence[ProjectionRecord] | None = None,
    players: Iterable[CanonicalPlayer] | None = None,
    stats_rows: object | None = None,
    season: int | None = None,
    num_teams: int | None = None,
    adp_source: str = "yahoo",
    schedule: SeasonSchedule | None = None,
    playoff_weight: float = 0.0,
) -> WarmResult:
    """Warm the store for one league: persist every input, then rebuild+store
    the value board. The one command of the load path.

    Args:
        store: The open :class:`CoachStore`.
        settings: The league's settings (live from Yahoo or built offline).
            ``settings.league_key`` keys the board snapshot.
        projection_source: Where projections come from when ``projections``
            isn't given — normally :class:`NflverseProjectionSource`, whose
            ``project()`` serves the step-1 warm cache with zero network. A
            failed pull is a warning, not an error.
        projections: Pre-fetched records (tests, or a caller that already
            pulled). Takes precedence over ``projection_source``.
        players: Canonical players from the M3 crosswalk, ideally with Yahoo
            ADP attached (``attach_yahoo_market``). ``None`` → identity rows
            are synthesized from projection metadata, fill-only.
        stats_rows: nflverse weekly rows (DataFrame or list of dicts) for
            ``stats_history``. ``None`` → prior rows are kept.
        season: Projection season; defaults to the upcoming one.
        num_teams: Override for offline settings without ``max_teams``.
        adp_source: Label for the ADP rows written from ``players``.
        schedule: Optional :class:`SeasonSchedule` (step 5) — the caller loads
            it (``ScheduleSource.load``, cache-served offline) like every other
            data object here. ``None`` → a pure season board, as before.
        playoff_weight: Blend weight for the stored board's draft values
            (0.0 keeps the board identical to the pre-step-5 output).

    Returns:
        A :class:`WarmResult` with per-table counts, refreshed scopes, and
        warnings for every degraded input.
    """
    season = season or default_season()
    league_key = settings.league_key or "unknown_league"
    result = WarmResult(league_key=league_key, season=season)

    # 1. League settings (always present — it's the argument).
    store.upsert_league_settings(settings, num_teams=num_teams)
    result.refreshed.append("league_settings")

    # 2. Projections: given > pulled (cache-served) > already stored.
    if projections is None and projection_source is not None:
        try:
            projections = projection_source.project(season=season)
        except Exception as exc:
            result.warnings.append(
                f"projection pull failed ({exc}); board will rebuild from stored rows"
            )
    if projections:
        # Carry the nflverse model's honesty label into the DB; other sources
        # can stamp their own note via upsert_projections directly.
        note = PROJECTION_NOTE if projections[0].source == "nflverse_model" else ""
        store.upsert_projections(projections, season=season, note=note)
        result.refreshed.append("projections")
    else:
        projections = store.projection_records(season=season)
        if projections:
            result.warnings.append(
                f"no fresh projections; using {len(projections)} stored rows for the board"
            )
        else:
            result.warnings.append(
                "no projections available (fresh or stored) — board not rebuilt"
            )

    # 3. Players + ADP. Real crosswalked players upsert fully; synthesized
    #    identity rows only fill gaps.
    if players is not None:
        players = list(players)
        store.upsert_players(players)
        result.refreshed.append("players")
        adp_rows = _adp_rows_from_players(players)
        if adp_rows:
            store.upsert_adp(adp_rows, source=adp_source)
            result.refreshed.append(f"adp:{adp_source}")
        else:
            result.warnings.append(
                "players carried no ADP (attach_yahoo_market not run?); keeping prior adp rows"
            )
    else:
        synthesized = _players_from_projections(projections or [])
        if synthesized:
            store.upsert_players(synthesized, fill_only=True)
        result.warnings.append(
            "no canonical players given (offline?); synthesized identity rows from "
            "projections, keeping prior player/adp rows"
        )

    # 4. Stats history.
    if stats_rows is not None:
        shaped = stats_rows_from_nflverse(stats_rows)
        if shaped:
            store.upsert_stats_history(shaped)
            result.refreshed.append("stats_history")
    else:
        result.warnings.append("stats history not provided; keeping prior rows")

    # 5. Rebuild + snapshot the board. Board-input players come from the STORE,
    #    not the call: that re-attaches persisted ADP (prior online warm) onto
    #    identity rows, which keeps the ADP→VORP gap-fill working offline.
    if projections:
        board_players = store.canonical_players()
        board = build_value_board(
            projections,
            settings,
            num_teams=num_teams,
            players=board_players or None,
            schedule=schedule,
            playoff_weight=playoff_weight,
        )
        result.board_entries = store.replace_board(league_key, board)
        result.skipped_no_signal = board.skipped_no_signal
        result.refreshed.append("value_board")

    result.counts = store.table_counts()
    return result
