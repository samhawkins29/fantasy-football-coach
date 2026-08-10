"""The local SQLite data store (framework §2.2 "SQLite first, zero-ops").

Everything the draft needs — league rules, canonical players, ADP,
projections, historical stats, and the computed value board — persisted into
one git-ignored ``.sqlite3`` file, populated by :func:`warm_store` pre-draft
and queryable from a REPL, a notebook, or raw SQL. The live draft loop (M5)
joins in via ``draft_picks`` + :meth:`CoachStore.top_available`.
"""

from fantasy_coach.store.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    STAT_HISTORY_STAT_COLUMNS,
    apply_migrations,
    schema_version,
)
from fantasy_coach.store.store import DEFAULT_DB_PATH, CoachStore
from fantasy_coach.store.warm import (
    WarmResult,
    stats_rows_from_nflverse,
    warm_store,
)

__all__ = [
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "STAT_HISTORY_STAT_COLUMNS",
    "apply_migrations",
    "schema_version",
    "DEFAULT_DB_PATH",
    "CoachStore",
    "WarmResult",
    "stats_rows_from_nflverse",
    "warm_store",
]
