"""``CoachStore`` — the queryable SQLite data store (framework §2.2 storage).

One class owns the single-file database: it opens/migrates the file, upserts
every data slice the pre-draft warm path produces (league settings, canonical
players, ADP, projections, stats history, the computed value board, draft
picks), and exposes the convenience read layer the founder actually analyses
with. Everything is **plain SQLite** — no ORM — so the same file works from a
Python REPL, a notebook (``pd.read_sql``), or the ``sqlite3`` CLI:

    >>> from fantasy_coach.store import CoachStore
    >>> store = CoachStore()                      # data/coach.sqlite3
    >>> store.get_board("449.l.123456", limit=15) # the stored VORP board
    >>> store.top_available("449.l.123456", 10)   # …minus drafted players (M5)
    >>> store.adp_vs_vorp("449.l.123456")         # market vs model value gaps
    >>> store.sql("SELECT position, COUNT(*) n FROM players GROUP BY 1")

Example raw SQL against the file (it is *just* SQLite):

    -- best remaining value by position tier
    SELECT position, tier, COUNT(*) players, ROUND(MAX(vorp), 1) best_vorp
    FROM value_board WHERE league_key = '449.l.123456'
    GROUP BY position, tier ORDER BY position, tier;

    -- players the market drafts later than our model ranks them (value falls)
    SELECT name, position, adp, overall_rank, ROUND(adp - overall_rank, 1) gap
    FROM value_board WHERE league_key = '449.l.123456' AND adp IS NOT NULL
    ORDER BY gap DESC LIMIT 20;

Design notes:

* **Upserts everywhere** (``ON CONFLICT … DO UPDATE``) so re-warming refreshes
  rows in place; the ``value_board`` is the one *snapshot* table — rebuilding a
  league's board replaces its rows wholesale so stale entries can't linger.
* **Vintage stamps**: every write path records ``data_vintage(scope,
  refreshed_at)`` so ``store.vintage()`` answers "how fresh is this?" at a
  glance — the founder's trust check before draft day.
* The ``now`` clock is injectable (the same seam as the throttle's
  :class:`FakeClock`) so tests pin timestamps exactly.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from fantasy_coach.clients.models import (
    DraftPick,
    LeagueSettings,
    RosterPosition,
    StatCategory,
)
from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
from fantasy_coach.ingest.injury import (
    DurabilityProfile,
    InjuryReport,
    merge_reports,
)
from fantasy_coach.ingest.sources import ProjectionRecord
from fantasy_coach.store.schema import (
    STAT_HISTORY_STAT_COLUMNS,
    apply_migrations,
)
from fantasy_coach.value.board import ValueBoard

__all__ = ["DEFAULT_DB_PATH", "CoachStore"]

#: Default single-file database location. ``*.sqlite3`` is git-ignored, so the
#: store never risks being committed.
DEFAULT_DB_PATH = "data/coach.sqlite3"

#: Tables :meth:`CoachStore.table_counts` reports, in schema order.
_TABLES = (
    "league_settings",
    "players",
    "adp",
    "projections",
    "stats_history",
    "value_board",
    "draft_picks",
    "injury_reports",
    "durability",
)


def _utc_now_iso() -> str:
    """Current UTC time as a second-resolution ISO string (the default clock)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CoachStore:
    """The single-file SQLite store, opened and migrated on construction.

    Args:
        path: Database file (parent directories are created). ``":memory:"``
            works for throwaway analysis or tests.
        now: Injectable clock returning an ISO timestamp string — every
            ``updated_at`` / vintage stamp goes through it.

    Usable as a context manager (``with CoachStore(...) as store:``); otherwise
    call :meth:`close` when done (or don't — SQLite survives that too).
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_DB_PATH,
        *,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self._now = now or _utc_now_iso
        if str(path) != ":memory:" and self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        apply_migrations(self.conn)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection."""
        self.conn.close()

    def __enter__(self) -> "CoachStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- generic access ------------------------------------------------------

    def sql(self, query: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Run arbitrary SQL and return the rows — the notebook escape hatch.

        Rows are :class:`sqlite3.Row`, so both ``row["vorp"]`` and ``dict(row)``
        work. Writes commit immediately.
        """
        with self.conn:
            return self.conn.execute(query, tuple(params)).fetchall()

    def table_counts(self) -> dict[str, int]:
        """``{table: row count}`` for every data table — the freshness overview."""
        return {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in _TABLES
        }

    def stamp_vintage(self, scope: str, detail: str = "") -> None:
        """Record that ``scope`` (e.g. ``"projections:nflverse_model:2026"``)
        was refreshed now."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO data_vintage (scope, refreshed_at, detail)
                VALUES (?, ?, ?)
                ON CONFLICT (scope) DO UPDATE SET
                  refreshed_at = excluded.refreshed_at,
                  detail = excluded.detail
                """,
                (scope, self._now(), detail),
            )

    def vintage(self) -> list[sqlite3.Row]:
        """Every data scope with its last-refreshed timestamp, oldest first."""
        return self.conn.execute(
            "SELECT scope, refreshed_at, detail FROM data_vintage ORDER BY refreshed_at, scope"
        ).fetchall()

    # -- league settings -----------------------------------------------------

    def upsert_league_settings(
        self, settings: LeagueSettings, *, num_teams: int | None = None
    ) -> None:
        """Persist a league's settled settings (scoring + roster as JSON).

        ``num_teams`` overrides ``settings.max_teams`` for offline-built
        settings that lack a team count.
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO league_settings (
                  league_key, scoring_type, draft_type, num_teams, is_superflex,
                  is_auction, uses_faab, stat_modifiers, roster_positions, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (league_key) DO UPDATE SET
                  scoring_type = excluded.scoring_type,
                  draft_type = excluded.draft_type,
                  num_teams = excluded.num_teams,
                  is_superflex = excluded.is_superflex,
                  is_auction = excluded.is_auction,
                  uses_faab = excluded.uses_faab,
                  stat_modifiers = excluded.stat_modifiers,
                  roster_positions = excluded.roster_positions,
                  updated_at = excluded.updated_at
                """,
                (
                    settings.league_key,
                    settings.scoring_type,
                    settings.draft_type,
                    num_teams or settings.max_teams,
                    int(settings.is_superflex),
                    int(settings.is_auction_draft),
                    int(settings.uses_faab),
                    json.dumps({str(k): v for k, v in settings.stat_modifiers().items()}),
                    json.dumps(
                        [
                            {
                                "position": slot.position,
                                "count": slot.count,
                                "is_starting_position": slot.is_starting_position,
                            }
                            for slot in settings.roster_positions
                        ]
                    ),
                    self._now(),
                ),
            )
        self.stamp_vintage(f"league_settings:{settings.league_key}")

    def league_settings(self, league_key: str) -> LeagueSettings | None:
        """Rebuild a :class:`LeagueSettings` from its stored row (or ``None``).

        The reconstruction carries everything the value engine consumes —
        scoring modifiers, roster slots, team count, draft type — which is the
        round-trip the store guarantees (not every raw Yahoo field).
        """
        row = self.conn.execute(
            "SELECT * FROM league_settings WHERE league_key = ?", (league_key,)
        ).fetchone()
        if row is None:
            return None
        return LeagueSettings(
            league_key=row["league_key"],
            scoring_type=row["scoring_type"],
            draft_type=row["draft_type"],
            is_auction_draft=bool(row["is_auction"]),
            uses_faab=bool(row["uses_faab"]),
            max_teams=row["num_teams"],
            roster_positions=[
                RosterPosition(
                    position=slot["position"],
                    count=slot["count"],
                    is_starting_position=slot["is_starting_position"],
                )
                for slot in json.loads(row["roster_positions"])
            ],
            stat_categories=[
                StatCategory(stat_id=int(stat_id), value=value)
                for stat_id, value in json.loads(row["stat_modifiers"]).items()
            ],
        )

    # -- players -------------------------------------------------------------

    def upsert_players(
        self, players: Iterable[CanonicalPlayer], *, fill_only: bool = False
    ) -> int:
        """Upsert canonical players; return how many rows were written.

        ``fill_only=True`` inserts only players *not already stored* — the
        degraded path for identity rows synthesized from projection metadata,
        which must never overwrite richer Yahoo-crosswalked rows (they carry
        yahoo ids, byes, and statuses the synthesis lacks).
        """
        conflict = (
            "DO NOTHING"
            if fill_only
            else """DO UPDATE SET
                  gsis_id = excluded.gsis_id,
                  yahoo_id = excluded.yahoo_id,
                  sleeper_id = excluded.sleeper_id,
                  name = excluded.name,
                  clean_name = excluded.clean_name,
                  position = excluded.position,
                  team = excluded.team,
                  bye_week = excluded.bye_week,
                  status = excluded.status,
                  is_defense = excluded.is_defense,
                  resolution_method = excluded.resolution_method,
                  updated_at = excluded.updated_at"""
        )
        written = 0
        with self.conn:
            for p in players:
                cursor = self.conn.execute(
                    f"""
                    INSERT INTO players (
                      canonical_id, gsis_id, yahoo_id, sleeper_id, name, clean_name,
                      position, team, bye_week, status, is_defense,
                      resolution_method, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (canonical_id) {conflict}
                    """,
                    (
                        p.canonical_id,
                        p.ids.gsis_id,
                        p.ids.yahoo_id,
                        p.ids.sleeper_id,
                        p.name,
                        p.clean_name,
                        p.position,
                        p.team,
                        p.bye_week,
                        p.status,
                        int(p.is_defense),
                        p.resolution_method,
                        self._now(),
                    ),
                )
                written += cursor.rowcount
        if written:
            self.stamp_vintage("players")
        return written

    def canonical_players(self, *, adp_source: str | None = None) -> list[CanonicalPlayer]:
        """Rebuild :class:`CanonicalPlayer` records with stored ADP attached.

        This is what makes persisted ADP *usable*: the board's ADP→VORP curve
        and gap-fill entries need players whose ``market.adp`` is populated,
        and this reconstruction survives an offline re-warm (the ADP came from
        a previous online warm). ``adp_source`` pins one source; otherwise the
        first source (alphabetically) with a pick for the player wins.
        """
        adp_rows = self.conn.execute(
            "SELECT canonical_id, source, average_pick, stdev FROM adp ORDER BY source"
        ).fetchall()
        adp_by_player: dict[str, sqlite3.Row] = {}
        for row in adp_rows:
            if adp_source is not None and row["source"] != adp_source:
                continue
            adp_by_player.setdefault(row["canonical_id"], row)

        players = []
        for row in self.conn.execute("SELECT * FROM players").fetchall():
            player = CanonicalPlayer(
                canonical_id=row["canonical_id"],
                ids=ExternalIds(
                    gsis_id=row["gsis_id"],
                    yahoo_id=row["yahoo_id"],
                    sleeper_id=row["sleeper_id"],
                ),
                name=row["name"],
                clean_name=row["clean_name"],
                position=row["position"],
                team=row["team"],
                bye_week=row["bye_week"],
                status=row["status"],
                is_defense=bool(row["is_defense"]),
                resolution_method=row["resolution_method"],
            )
            adp = adp_by_player.get(row["canonical_id"])
            if adp is not None and adp["average_pick"] is not None:
                player.market.adp = adp["average_pick"]
                player.market.adp_stddev = adp["stdev"]
            players.append(player)
        return players

    def players_by_position(self, position: str) -> list[sqlite3.Row]:
        """Stored player rows at a position, alphabetically."""
        return self.conn.execute(
            "SELECT * FROM players WHERE position = ? ORDER BY name",
            (position.strip().upper(),),
        ).fetchall()

    # -- ADP -----------------------------------------------------------------

    def upsert_adp(self, rows: Iterable[Mapping[str, object]], *, source: str) -> int:
        """Upsert ADP rows for one source; return the count written.

        Each row needs ``canonical_id`` and ``average_pick``; ``average_round``,
        ``stdev`` and ``percent_drafted`` are optional. Rows without a pick are
        skipped (no signal ≠ pick 0). Existing rows for *other* players keep
        their prior values — a partial refresh never wipes the table.
        """
        written = 0
        with self.conn:
            for row in rows:
                if row.get("average_pick") is None:
                    continue
                self.conn.execute(
                    """
                    INSERT INTO adp (
                      canonical_id, source, average_pick, average_round,
                      stdev, percent_drafted, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (canonical_id, source) DO UPDATE SET
                      average_pick = excluded.average_pick,
                      average_round = excluded.average_round,
                      stdev = excluded.stdev,
                      percent_drafted = excluded.percent_drafted,
                      updated_at = excluded.updated_at
                    """,
                    (
                        str(row["canonical_id"]),
                        source,
                        row["average_pick"],
                        row.get("average_round"),
                        row.get("stdev"),
                        row.get("percent_drafted"),
                        self._now(),
                    ),
                )
                written += 1
        if written:
            self.stamp_vintage(f"adp:{source}")
        return written

    # -- projections ---------------------------------------------------------

    def upsert_projections(
        self,
        records: Iterable[ProjectionRecord],
        *,
        season: int,
        horizon: str = "season",
        note: str = "",
    ) -> int:
        """Upsert projection records (keyed by player+source+horizon+season)."""
        written = 0
        source = ""
        with self.conn:
            for rec in records:
                source = rec.source
                self.conn.execute(
                    """
                    INSERT INTO projections (
                      canonical_id, source, horizon, season, points,
                      position, team, name, stats, note, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (canonical_id, source, horizon, season) DO UPDATE SET
                      points = excluded.points,
                      position = excluded.position,
                      team = excluded.team,
                      name = excluded.name,
                      stats = excluded.stats,
                      note = excluded.note,
                      updated_at = excluded.updated_at
                    """,
                    (
                        rec.source_id,
                        rec.source,
                        horizon,
                        season,
                        rec.points,
                        rec.position,
                        rec.team,
                        rec.name,
                        json.dumps(rec.stats),
                        note,
                        self._now(),
                    ),
                )
                written += 1
        if written:
            self.stamp_vintage(f"projections:{source}:{season}", detail=note)
        return written

    def projection_records(
        self,
        *,
        season: int | None = None,
        source: str | None = None,
        horizon: str = "season",
    ) -> list[ProjectionRecord]:
        """Rebuild :class:`ProjectionRecord` objects from stored rows.

        This is the offline board-rebuild path: filter by season/source (or
        take everything at the horizon) and feed the result straight back into
        :func:`~fantasy_coach.value.board.build_value_board`.
        """
        query = "SELECT * FROM projections WHERE horizon = ?"
        params: list[object] = [horizon]
        if season is not None:
            query += " AND season = ?"
            params.append(season)
        if source is not None:
            query += " AND source = ?"
            params.append(source)
        return [
            ProjectionRecord(
                source=row["source"],
                source_id=row["canonical_id"],
                source_id_field="gsis_id",
                points=row["points"],
                position=row["position"],
                team=row["team"],
                name=row["name"],
                stats={k: float(v) for k, v in json.loads(row["stats"]).items()},
            )
            for row in self.conn.execute(query + " ORDER BY points DESC", params)
        ]

    # -- stats history -------------------------------------------------------

    def upsert_stats_history(self, rows: Iterable[Mapping[str, object]]) -> int:
        """Upsert weekly stat lines (already store-shaped — see ``warm.py``).

        Each row carries ``canonical_id`` / ``season`` / ``week`` plus any of
        the :data:`~fantasy_coach.store.schema.STAT_HISTORY_STAT_COLUMNS`;
        missing stat columns default to 0.
        """
        stat_cols = ", ".join(STAT_HISTORY_STAT_COLUMNS)
        stat_updates = ", ".join(
            f"{c} = excluded.{c}" for c in STAT_HISTORY_STAT_COLUMNS
        )
        placeholders = ", ".join("?" for _ in range(6 + len(STAT_HISTORY_STAT_COLUMNS) + 1))
        written = 0
        with self.conn:
            for row in rows:
                values = [
                    str(row["canonical_id"]),
                    int(row["season"]),  # type: ignore[arg-type]
                    int(row["week"]),  # type: ignore[arg-type]
                    str(row.get("season_type", "REG")),
                    str(row.get("position", "")),
                    str(row.get("team", "")),
                    *(float(row.get(c, 0.0) or 0.0) for c in STAT_HISTORY_STAT_COLUMNS),  # type: ignore[arg-type]
                    self._now(),
                ]
                self.conn.execute(
                    f"""
                    INSERT INTO stats_history (
                      canonical_id, season, week, season_type, position, team,
                      {stat_cols}, updated_at
                    ) VALUES ({placeholders})
                    ON CONFLICT (canonical_id, season, week) DO UPDATE SET
                      season_type = excluded.season_type,
                      position = excluded.position,
                      team = excluded.team,
                      {stat_updates},
                      updated_at = excluded.updated_at
                    """,
                    values,
                )
                written += 1
        if written:
            self.stamp_vintage("stats_history")
        return written

    # -- injury reports + durability (step 6) ----------------------------------

    def upsert_injury_reports(
        self, reports: Mapping[str, InjuryReport], *, source: str
    ) -> int:
        """Upsert one source's current designations (``{canonical_id: report}``).

        Healthy reports are stored too — a fresh "no designation" is what
        clears a stale flag in the merge. A report without ``fetched_at`` is
        stamped with the store clock at write time. Returns the count written
        and stamps ``injury_status:{source}`` vintage.
        """
        written = 0
        with self.conn:
            for cid, report in reports.items():
                self.conn.execute(
                    """
                    INSERT INTO injury_reports (
                      canonical_id, source, status, raw_status, detail,
                      practice, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (canonical_id, source) DO UPDATE SET
                      status = excluded.status,
                      raw_status = excluded.raw_status,
                      detail = excluded.detail,
                      practice = excluded.practice,
                      fetched_at = excluded.fetched_at
                    """,
                    (
                        cid,
                        source,
                        report.status,
                        report.raw_status,
                        report.detail,
                        report.practice,
                        report.fetched_at or self._now(),
                    ),
                )
                written += 1
        if written:
            self.stamp_vintage(f"injury_status:{source}")
        return written

    def injury_reports_by_source(self) -> dict[str, dict[str, InjuryReport]]:
        """Every stored designation, grouped ``{source: {canonical_id: report}}``.

        The live loop seeds its per-source picture from this so a mid-draft
        Sleeper re-check replaces only the Sleeper slice.
        """
        out: dict[str, dict[str, InjuryReport]] = {}
        for row in self.conn.execute("SELECT * FROM injury_reports").fetchall():
            out.setdefault(row["source"], {})[row["canonical_id"]] = InjuryReport(
                source=row["source"],
                status=row["status"],
                raw_status=row["raw_status"],
                detail=row["detail"],
                practice=row["practice"],
                fetched_at=row["fetched_at"],
            )
        return out

    def injury_reports(self, *, source: str | None = None) -> dict[str, InjuryReport]:
        """Stored designations as ``{canonical_id: report}``.

        With no ``source``, multi-source rows are collapsed per player with the
        fresh-beats-stale / severe-beats-mild rule
        (:func:`~fantasy_coach.ingest.injury.merge_reports`) — the board's
        input. ``source=...`` returns that one source's rows unmerged.
        """
        by_source = self.injury_reports_by_source()
        if source is not None:
            return by_source.get(source, {})
        by_player: dict[str, list[InjuryReport]] = {}
        for reports in by_source.values():
            for cid, report in reports.items():
                by_player.setdefault(cid, []).append(report)
        out: dict[str, InjuryReport] = {}
        for cid, reports in by_player.items():
            winner = merge_reports(reports)
            if winner is not None:
                out[cid] = winner
        return out

    def upsert_durability(self, profiles: Iterable[DurabilityProfile]) -> int:
        """Upsert per-player durability profiles; return the count written."""
        written = 0
        with self.conn:
            for p in profiles:
                self.conn.execute(
                    """
                    INSERT INTO durability (
                      canonical_id, name, position, games, avg_missed,
                      seasons_seen, designations, soft_tissue, risk, discount,
                      note, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (canonical_id) DO UPDATE SET
                      name = excluded.name,
                      position = excluded.position,
                      games = excluded.games,
                      avg_missed = excluded.avg_missed,
                      seasons_seen = excluded.seasons_seen,
                      designations = excluded.designations,
                      soft_tissue = excluded.soft_tissue,
                      risk = excluded.risk,
                      discount = excluded.discount,
                      note = excluded.note,
                      updated_at = excluded.updated_at
                    """,
                    (
                        p.canonical_id,
                        p.name,
                        p.position,
                        json.dumps({str(y): g for y, g in p.games.items()}),
                        p.avg_missed,
                        p.seasons_seen,
                        p.designations,
                        p.soft_tissue,
                        p.risk,
                        p.discount,
                        p.note,
                        self._now(),
                    ),
                )
                written += 1
        if written:
            self.stamp_vintage("durability")
        return written

    def durability_profiles(self) -> dict[str, DurabilityProfile]:
        """Stored durability profiles as ``{canonical_id: profile}``."""
        out: dict[str, DurabilityProfile] = {}
        for row in self.conn.execute("SELECT * FROM durability").fetchall():
            out[row["canonical_id"]] = DurabilityProfile(
                canonical_id=row["canonical_id"],
                name=row["name"],
                position=row["position"],
                games={int(y): float(g) for y, g in json.loads(row["games"]).items()},
                avg_missed=row["avg_missed"],
                seasons_seen=row["seasons_seen"],
                designations=row["designations"],
                soft_tissue=row["soft_tissue"],
                risk=row["risk"],
                discount=row["discount"],
                note=row["note"],
            )
        return out

    # -- value board (snapshot semantics) --------------------------------------

    def replace_board(self, league_key: str, board: ValueBoard) -> int:
        """Replace a league's stored board with a freshly built one.

        Snapshot, not upsert: entries that fell off the board (players who lost
        their projection or signal) must disappear from the table too, so the
        league's old rows are deleted in the same transaction.
        """
        built_at = self._now()
        with self.conn:
            self.conn.execute(
                "DELETE FROM value_board WHERE league_key = ?", (league_key,)
            )
            for e in board.entries:
                self.conn.execute(
                    """
                    INSERT INTO value_board (
                      league_key, canonical_id, name, position, team, points, vorp,
                      draft_value, playoff_vorp, schedule_note,
                      injury_status, durability_risk, injury_note, injury_discount,
                      overall_rank, pos_rank, tier, value_source, adp, bye_week, built_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        league_key,
                        e.canonical_id,
                        e.name,
                        e.position,
                        e.team,
                        e.points,
                        e.vorp,
                        e.draft_value,
                        e.playoff_vorp,
                        e.schedule_note,
                        e.injury_status,
                        e.durability_risk,
                        e.injury_note,
                        e.injury_discount,
                        e.overall_rank,
                        e.pos_rank,
                        e.tier,
                        e.value_source,
                        e.adp,
                        e.bye_week,
                        built_at,
                    ),
                )
            self.conn.execute(
                """
                INSERT INTO board_meta (
                  league_key, num_teams, baselines, scoring, skipped_no_signal,
                  playoff_weight, playoff_weeks, injury_weight, built_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (league_key) DO UPDATE SET
                  num_teams = excluded.num_teams,
                  baselines = excluded.baselines,
                  scoring = excluded.scoring,
                  skipped_no_signal = excluded.skipped_no_signal,
                  playoff_weight = excluded.playoff_weight,
                  playoff_weeks = excluded.playoff_weeks,
                  injury_weight = excluded.injury_weight,
                  built_at = excluded.built_at
                """,
                (
                    league_key,
                    board.num_teams,
                    json.dumps(board.baselines),
                    json.dumps(board.scoring),
                    board.skipped_no_signal,
                    board.playoff_weight,
                    json.dumps(board.playoff_weeks),
                    board.injury_weight,
                    built_at,
                ),
            )
        self.stamp_vintage(f"value_board:{league_key}")
        return len(board.entries)

    def get_board(
        self,
        league_key: str,
        *,
        position: str | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """The stored board in overall-rank order (optionally one position)."""
        query = "SELECT * FROM value_board WHERE league_key = ?"
        params: list[object] = [league_key]
        if position is not None:
            query += " AND position = ?"
            params.append(position.strip().upper())
        query += " ORDER BY overall_rank"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(query, params).fetchall()

    def board_meta(self, league_key: str) -> sqlite3.Row | None:
        """The baselines/scoring/num_teams the stored board was built under."""
        return self.conn.execute(
            "SELECT * FROM board_meta WHERE league_key = ?", (league_key,)
        ).fetchone()

    # -- draft picks (M5 writes; the available-board query excludes) -----------

    def record_draft_picks(self, league_key: str, picks: Iterable[DraftPick]) -> int:
        """Upsert *made* draft picks; return the count recorded.

        Yahoo returns the whole pick list up front with empty ``player_key``
        for picks not yet made — only made picks are stored, so the table is
        exactly "who is gone". The canonical id is resolved through the stored
        players' yahoo ids at write time; an unknown yahoo id stores ``NULL``
        (the pick still counts, exclusion then falls back to the yahoo id).
        """
        written = 0
        with self.conn:
            for pick in picks:
                if not pick.is_made:
                    continue
                yahoo_id = pick.player_id
                row = self.conn.execute(
                    "SELECT canonical_id FROM players WHERE yahoo_id = ?", (yahoo_id,)
                ).fetchone()
                self.conn.execute(
                    """
                    INSERT INTO draft_picks (
                      league_key, pick, round, team_key, player_key,
                      yahoo_player_id, canonical_id, cost, made_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (league_key, pick) DO UPDATE SET
                      round = excluded.round,
                      team_key = excluded.team_key,
                      player_key = excluded.player_key,
                      yahoo_player_id = excluded.yahoo_player_id,
                      canonical_id = excluded.canonical_id,
                      cost = excluded.cost,
                      made_at = excluded.made_at
                    """,
                    (
                        league_key,
                        pick.pick,
                        pick.round,
                        pick.team_key,
                        pick.player_key,
                        yahoo_id,
                        row["canonical_id"] if row else None,
                        pick.cost,
                        self._now(),
                    ),
                )
                written += 1
        return written

    def clear_draft_picks(self, league_key: str) -> int:
        """Delete a league's recorded picks; return how many were removed.

        The M5 live loop mirrors Yahoo's pick list with **rebuild semantics**
        (clear + record every change) so a commissioner undo or auto-pick
        correction can't leave a stale row claiming a player is gone.
        """
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM draft_picks WHERE league_key = ?", (league_key,)
            )
        return cursor.rowcount

    def drafted_canonical_ids(self, league_key: str) -> set[str]:
        """Canonical ids already taken in a league's draft (the M5 hot set).

        Picks whose yahoo id never resolved fall back to matching through the
        players table at read time (the player may have been crosswalked since).
        """
        rows = self.conn.execute(
            """
            SELECT COALESCE(dp.canonical_id, p.canonical_id) AS cid
            FROM draft_picks dp
            LEFT JOIN players p ON p.yahoo_id = dp.yahoo_player_id
            WHERE dp.league_key = ? AND dp.player_key != ''
            """,
            (league_key,),
        ).fetchall()
        return {row["cid"] for row in rows if row["cid"] is not None}

    def top_available(
        self,
        league_key: str,
        n: int = 15,
        *,
        position: str | None = None,
    ) -> list[sqlite3.Row]:
        """The best still-undrafted board entries — M5's core read.

        Board order minus everyone in ``draft_picks``; before any picks exist
        it is simply the top of the board.
        """
        query = """
            SELECT vb.* FROM value_board vb
            WHERE vb.league_key = :lk
              AND vb.canonical_id NOT IN (
                SELECT COALESCE(dp.canonical_id, p.canonical_id)
                FROM draft_picks dp
                LEFT JOIN players p ON p.yahoo_id = dp.yahoo_player_id
                WHERE dp.league_key = :lk AND dp.player_key != ''
                  AND COALESCE(dp.canonical_id, p.canonical_id) IS NOT NULL
              )
        """
        params: dict[str, object] = {"lk": league_key, "n": n}
        if position is not None:
            query += " AND vb.position = :pos"
            params["pos"] = position.strip().upper()
        query += " ORDER BY vb.overall_rank LIMIT :n"
        return self.conn.execute(query, params).fetchall()

    # -- analysis reads --------------------------------------------------------

    def adp_vs_vorp(
        self, league_key: str, *, limit: int | None = None
    ) -> list[sqlite3.Row]:
        """Market-vs-model value gaps: ``gap = adp − overall_rank``.

        Positive gap → the market drafts the player *later* than our board
        ranks them (likely value that falls to you); negative → the market
        reaches earlier than our model justifies. Sorted best value first.
        """
        query = """
            SELECT name, position, team, value_source, adp, vorp, overall_rank,
                   ROUND(adp - overall_rank, 1) AS gap
            FROM value_board
            WHERE league_key = ? AND adp IS NOT NULL
            ORDER BY gap DESC
        """
        params: list[object] = [league_key]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(query, params).fetchall()

    def player_summary(self, name_or_id: str) -> dict[str, object] | None:
        """Everything stored about one player, for REPL spelunking.

        Accepts a canonical id or a (partial, case-insensitive) name. Returns
        ``{"player": Row, "adp": [Row], "projections": [Row], "board":
        [Row], "stats_history": [Row]}`` or ``None`` if no player matches.
        """
        player = self.conn.execute(
            "SELECT * FROM players WHERE canonical_id = ?", (name_or_id,)
        ).fetchone()
        if player is None:
            player = self.conn.execute(
                "SELECT * FROM players WHERE name LIKE ? ORDER BY name LIMIT 1",
                (f"%{name_or_id}%",),
            ).fetchone()
        if player is None:
            return None
        cid = player["canonical_id"]
        return {
            "player": player,
            "adp": self.conn.execute(
                "SELECT * FROM adp WHERE canonical_id = ?", (cid,)
            ).fetchall(),
            "projections": self.conn.execute(
                "SELECT * FROM projections WHERE canonical_id = ? ORDER BY season DESC",
                (cid,),
            ).fetchall(),
            "board": self.conn.execute(
                "SELECT * FROM value_board WHERE canonical_id = ? ORDER BY league_key",
                (cid,),
            ).fetchall(),
            "stats_history": self.conn.execute(
                "SELECT * FROM stats_history WHERE canonical_id = ? "
                "ORDER BY season DESC, week DESC",
                (cid,),
            ).fetchall(),
        }
