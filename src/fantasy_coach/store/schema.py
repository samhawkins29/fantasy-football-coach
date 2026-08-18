"""SQLite schema + idempotent migrations for the Fantasy Coach data store.

The framework picks **SQLite first** (§2.2 "one-file, zero-ops; season data is
small") — the store is a single git-ignored ``.sqlite3`` file the founder can
open with any SQLite tool, query from a notebook, or throw away and re-warm.

Migration pattern: :data:`MIGRATIONS` is an append-only tuple of DDL batches;
``PRAGMA user_version`` records how many have been applied. Opening a store
applies only the batches beyond the recorded version, so re-opening an
up-to-date file is a no-op and an old file upgrades in place. Every statement
is additionally ``IF NOT EXISTS``-guarded, so even a version-counter mishap
cannot make schema creation fail — creation is idempotent twice over.

Schema map (what each table is *for*):

* ``league_settings`` — one row per league: scoring type, team count,
  superflex flag, and the full ``stat_modifiers`` / ``roster_positions`` as
  JSON so :class:`~fantasy_coach.clients.models.LeagueSettings` round-trips.
* ``players`` — the canonical player layer (§3.3), keyed by ``canonical_id``
  (gsis hub / ``DST_*`` / ``UNK_*``) with the Yahoo/Sleeper spoke ids.
* ``adp`` — per-(player, source) market ADP. **Persisted deliberately**: the
  value board's ADP→VORP gap-fill curve needs projection-backed players *with*
  ADP, so an offline re-warm must be able to rejoin prior ADP onto players.
* ``projections`` — per-(player, source, horizon, season) stat-line
  projections: raw component ``stats`` JSON + the source's reference points.
* ``stats_history`` — historical weekly box/usage lines from nflverse, the raw
  material for any statistical analysis the projections didn't pre-chew.
* ``value_board`` + ``board_meta`` — the computed league-rescored VORP board
  (M4 output) as a queryable snapshot, plus the baselines/scoring it assumed.
* ``draft_picks`` — live draft results (M5 writes these; the "available
  players" query excludes them).
* ``data_vintage`` — one refresh timestamp per data scope, so the founder
  always knows how fresh each slice is.
* ``injury_reports`` — the latest injury designation per (player, source)
  (Yahoo live / Sleeper), merged at read time; every row keeps its vintage.
* ``durability`` — per-player games-missed history + the clamped availability
  discount and categorical risk flag (a labelled model estimate).

v4 widens ``projections`` with the floor/ceiling distribution and
``value_board``/``board_meta`` with the floor/ceiling VORPs, the per-week SOS
value, and the risk-preference / SOS dials the board was built with.
"""

from __future__ import annotations

import sqlite3

__all__ = [
    "SCHEMA_VERSION",
    "MIGRATIONS",
    "STAT_HISTORY_STAT_COLUMNS",
    "apply_migrations",
    "schema_version",
]

#: The box/usage stat columns ``stats_history`` carries. This list is frozen
#: into the v1 DDL below — ``CREATE TABLE IF NOT EXISTS`` cannot add columns to
#: an existing file, so widening it later must be a *new* migration batch. A
#: test asserts it stays a superset of ``PROJECTED_STAT_KEYS`` to catch drift.
STAT_HISTORY_STAT_COLUMNS: tuple[str, ...] = (
    "pass_yds",
    "pass_td",
    "pass_int",
    "rush_yds",
    "rush_td",
    "rec",
    "rec_yds",
    "rec_td",
    "fum_lost",
    "two_pt",
    "carries",
    "targets",
)

_STAT_COLS_DDL = ",\n  ".join(
    f"{col} REAL NOT NULL DEFAULT 0" for col in STAT_HISTORY_STAT_COLUMNS
)

#: Append-only DDL batches. Batch N is applied when ``user_version < N``.
#: NEVER edit a shipped batch — append a new one (that is the whole pattern).
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # -- v1: the full pre-draft store ----------------------------------------
    (
        """
        CREATE TABLE IF NOT EXISTS league_settings (
          league_key       TEXT PRIMARY KEY,
          scoring_type     TEXT NOT NULL DEFAULT '',
          draft_type       TEXT NOT NULL DEFAULT '',
          num_teams        INTEGER,
          is_superflex     INTEGER NOT NULL DEFAULT 0,
          is_auction       INTEGER NOT NULL DEFAULT 0,
          uses_faab        INTEGER NOT NULL DEFAULT 0,
          stat_modifiers   TEXT NOT NULL DEFAULT '{}',
          roster_positions TEXT NOT NULL DEFAULT '[]',
          updated_at       TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS players (
          canonical_id      TEXT PRIMARY KEY,
          gsis_id           TEXT,
          yahoo_id          TEXT,
          sleeper_id        TEXT,
          name              TEXT NOT NULL DEFAULT '',
          clean_name        TEXT NOT NULL DEFAULT '',
          position          TEXT NOT NULL DEFAULT '',
          team              TEXT NOT NULL DEFAULT '',
          bye_week          INTEGER,
          status            TEXT NOT NULL DEFAULT '',
          is_defense        INTEGER NOT NULL DEFAULT 0,
          resolution_method TEXT NOT NULL DEFAULT '',
          updated_at        TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_players_yahoo ON players (yahoo_id)",
        "CREATE INDEX IF NOT EXISTS idx_players_position ON players (position)",
        """
        CREATE TABLE IF NOT EXISTS adp (
          canonical_id    TEXT NOT NULL REFERENCES players (canonical_id),
          source          TEXT NOT NULL,
          average_pick    REAL,
          average_round   REAL,
          stdev           REAL,
          percent_drafted REAL,
          updated_at      TEXT NOT NULL,
          PRIMARY KEY (canonical_id, source)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS projections (
          canonical_id TEXT NOT NULL,
          source       TEXT NOT NULL,
          horizon      TEXT NOT NULL DEFAULT 'season',
          season       INTEGER NOT NULL,
          points       REAL NOT NULL DEFAULT 0,
          position     TEXT NOT NULL DEFAULT '',
          team         TEXT NOT NULL DEFAULT '',
          name         TEXT NOT NULL DEFAULT '',
          stats        TEXT NOT NULL DEFAULT '{}',
          note         TEXT NOT NULL DEFAULT '',
          updated_at   TEXT NOT NULL,
          PRIMARY KEY (canonical_id, source, horizon, season)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS stats_history (
          canonical_id TEXT NOT NULL,
          season       INTEGER NOT NULL,
          week         INTEGER NOT NULL,
          season_type  TEXT NOT NULL DEFAULT 'REG',
          position     TEXT NOT NULL DEFAULT '',
          team         TEXT NOT NULL DEFAULT '',
          {_STAT_COLS_DDL},
          updated_at   TEXT NOT NULL,
          PRIMARY KEY (canonical_id, season, week)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_stats_history_season ON stats_history (season, week)",
        """
        CREATE TABLE IF NOT EXISTS value_board (
          league_key   TEXT NOT NULL REFERENCES league_settings (league_key),
          canonical_id TEXT NOT NULL,
          name         TEXT NOT NULL DEFAULT '',
          position     TEXT NOT NULL DEFAULT '',
          team         TEXT NOT NULL DEFAULT '',
          points       REAL,
          vorp         REAL NOT NULL DEFAULT 0,
          overall_rank INTEGER NOT NULL,
          pos_rank     INTEGER NOT NULL DEFAULT 0,
          tier         INTEGER NOT NULL DEFAULT 0,
          value_source TEXT NOT NULL DEFAULT 'projection',
          adp          REAL,
          bye_week     INTEGER,
          built_at     TEXT NOT NULL,
          PRIMARY KEY (league_key, canonical_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_value_board_rank ON value_board (league_key, overall_rank)",
        """
        CREATE TABLE IF NOT EXISTS board_meta (
          league_key        TEXT PRIMARY KEY REFERENCES league_settings (league_key),
          num_teams         INTEGER NOT NULL,
          baselines         TEXT NOT NULL DEFAULT '{}',
          scoring           TEXT NOT NULL DEFAULT '{}',
          skipped_no_signal INTEGER NOT NULL DEFAULT 0,
          built_at          TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS draft_picks (
          league_key      TEXT NOT NULL,
          pick            INTEGER NOT NULL,
          round           INTEGER NOT NULL DEFAULT 0,
          team_key        TEXT NOT NULL DEFAULT '',
          player_key      TEXT NOT NULL DEFAULT '',
          yahoo_player_id TEXT NOT NULL DEFAULT '',
          canonical_id    TEXT,
          cost            INTEGER,
          made_at         TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (league_key, pick)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS data_vintage (
          scope        TEXT PRIMARY KEY,
          refreshed_at TEXT NOT NULL,
          detail       TEXT NOT NULL DEFAULT ''
        )
        """,
    ),
    # -- v2: schedule-aware board (step 5) — playoff/blended value columns ----
    # ALTER TABLE ADD COLUMN has no IF NOT EXISTS in SQLite; apply_migrations
    # tolerates "duplicate column name" so a crash mid-batch stays re-runnable.
    (
        "ALTER TABLE value_board ADD COLUMN draft_value REAL",
        "ALTER TABLE value_board ADD COLUMN playoff_vorp REAL",
        "ALTER TABLE value_board ADD COLUMN schedule_note TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE board_meta ADD COLUMN playoff_weight REAL NOT NULL DEFAULT 0",
        "ALTER TABLE board_meta ADD COLUMN playoff_weeks TEXT NOT NULL DEFAULT '[]'",
    ),
    # -- v3: injury/durability risk (step 6) ----------------------------------
    # ``injury_reports`` keeps the latest designation *per source* so the merge
    # (fresh beats stale, severe beats mild) can re-run at read time and every
    # source's vintage stays independently visible. ``durability`` is one row
    # per player — the transparent games-missed signal + clamped discount.
    (
        "ALTER TABLE value_board ADD COLUMN injury_status TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE value_board ADD COLUMN durability_risk TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE value_board ADD COLUMN injury_note TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE value_board ADD COLUMN injury_discount REAL",
        "ALTER TABLE board_meta ADD COLUMN injury_weight REAL NOT NULL DEFAULT 0",
        """
        CREATE TABLE IF NOT EXISTS injury_reports (
          canonical_id TEXT NOT NULL,
          source       TEXT NOT NULL,
          status       TEXT NOT NULL DEFAULT '',
          raw_status   TEXT NOT NULL DEFAULT '',
          detail       TEXT NOT NULL DEFAULT '',
          practice     TEXT NOT NULL DEFAULT '',
          fetched_at   TEXT NOT NULL,
          PRIMARY KEY (canonical_id, source)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS durability (
          canonical_id TEXT PRIMARY KEY,
          name         TEXT NOT NULL DEFAULT '',
          position     TEXT NOT NULL DEFAULT '',
          games        TEXT NOT NULL DEFAULT '{}',
          avg_missed   REAL NOT NULL DEFAULT 0,
          seasons_seen INTEGER NOT NULL DEFAULT 0,
          designations INTEGER NOT NULL DEFAULT 0,
          soft_tissue  INTEGER NOT NULL DEFAULT 0,
          risk         TEXT NOT NULL DEFAULT '',
          discount     REAL NOT NULL DEFAULT 0,
          note         TEXT NOT NULL DEFAULT '',
          updated_at   TEXT NOT NULL
        )
        """,
    ),
    # -- v4: projection distributions + per-week SOS (upgrades 1 & 3) --------
    (
        "ALTER TABLE projections ADD COLUMN floor REAL",
        "ALTER TABLE projections ADD COLUMN ceiling REAL",
        "ALTER TABLE value_board ADD COLUMN floor REAL",
        "ALTER TABLE value_board ADD COLUMN ceiling REAL",
        "ALTER TABLE value_board ADD COLUMN floor_vorp REAL",
        "ALTER TABLE value_board ADD COLUMN ceiling_vorp REAL",
        "ALTER TABLE value_board ADD COLUMN sos_vorp REAL",
        "ALTER TABLE board_meta ADD COLUMN risk_preference REAL NOT NULL DEFAULT 0",
        "ALTER TABLE board_meta ADD COLUMN sos_weight REAL NOT NULL DEFAULT 0",
    ),
)

#: The schema version an up-to-date store reports (``PRAGMA user_version``).
SCHEMA_VERSION = len(MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    """The migration batch count already applied to this database file."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring ``conn``'s database up to :data:`SCHEMA_VERSION`; return it.

    Applies only the batches beyond the file's recorded ``user_version`` —
    re-running against an up-to-date file executes nothing. Each batch commits
    atomically with its version bump, so a crash mid-migration re-runs that
    batch (safe: ``CREATE`` statements are ``IF NOT EXISTS``-guarded, and an
    ``ALTER TABLE ADD COLUMN`` hitting an already-added column is skipped —
    SQLite has no ``IF NOT EXISTS`` for columns, so the tolerance lives here).
    """
    version = schema_version(conn)
    for batch_number, statements in enumerate(MIGRATIONS, start=1):
        if batch_number <= version:
            continue
        with conn:  # one transaction per batch
            for statement in statements:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise
            conn.execute(f"PRAGMA user_version = {batch_number}")
    return schema_version(conn)
