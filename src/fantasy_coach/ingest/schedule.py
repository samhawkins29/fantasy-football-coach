"""NFL schedule + opponent-difficulty data (step-5 input, framework §2.5/§7).

Draft-time schedule awareness needs exactly two facts per player the projection
layer can't see:

* **When and whom their team plays** — the season's week-by-week opponent map
  and each team's bye week, from nflverse ``import_schedules`` (free, no key).
* **How hard each opponent is for their position** — a per-position
  opponent-difficulty multiplier derived from *last season's* fantasy points
  allowed by each defense to each position (nflverse weekly box scores, scored
  under :data:`~fantasy_coach.ingest.projections.REFERENCE_SCORING`), normalized
  to the league mean and clamped so it stays a nudge, never a rewrite.

Honesty posture, same as projections: last season's points-allowed is a **crude
proxy** for next season's defensive strength (coordinators change, rosters
turn over), so multipliers are clamped tight (default ``0.75–1.25``), every
cache file carries :data:`SCHEDULE_NOTE`, and downstream labels anything built
on this as a model estimate. Direction is the part we trust: a defense that
bled fantasy points to RBs last year is *more likely* soft vs RB than hard.

**Caching / draft-day.** Like projections, the computed schedule is cached as
JSON under ``cache_dir`` per season: call :meth:`ScheduleSource.warm_cache`
while online pre-draft, and :meth:`load` serves from the file with zero
network from then on (framework §7 "pre-draft warm cache").

The in-season weekly optimizer (a later step) reuses :class:`SeasonSchedule`
as-is — ``opponent()``/``multiplier()`` per week is exactly the start/sit and
rest-of-season input; only the *aggregation* here is draft-scoped.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from fantasy_coach.ingest.names import normalize_position, normalize_team
from fantasy_coach.ingest.projections import (
    PROJECTED_STAT_KEYS,
    REFERENCE_SCORING,
    score_stats,
)
from fantasy_coach.ingest.sources import NflverseSource

__all__ = [
    "SCHEDULE_NOTE",
    "SeasonSchedule",
    "ScheduleSource",
]

#: The honesty label stamped into every schedule cache file. Multipliers are a
#: prior-season points-allowed proxy — directionally useful, not a forecast.
SCHEDULE_NOTE = (
    "model estimate (nflverse-based): opponent difficulty = prior-season fantasy "
    "points allowed per position, normalized to league mean and clamped"
)

#: Positions the difficulty measure covers (the projectable ones — K/DEF have
#: no per-position points-allowed signal worth modelling here). For IDP groups
#: the "defense" charged is the opponent *offense* that yielded the tackles /
#: sacks — the same per-position points-allowed machinery, mirrored.
_SOS_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "DL", "LB", "DB"})

#: NFL regular-season weeks (18 since 2021; every team plays 17 + one bye).
_REG_SEASON_WEEKS = 18


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


@dataclass(slots=True)
class SeasonSchedule:
    """One season's opponent map, byes, and per-position opponent difficulty.

    Attributes:
        season: The season the ``opponents``/``byes`` describe.
        opponents: ``{team: {week: opponent}}`` for the regular season.
        byes: ``{team: bye_week}`` — the week a team does not play.
        multipliers: ``{position: {defense_team: multiplier}}`` where > 1.0
            means the defense was *easier* than average for that position
            (allowed more fantasy points) and < 1.0 harder. Missing entries
            read as neutral 1.0 — an honest "we don't know".
        sos_seasons: The completed seasons the multipliers were computed from
            (empty when no points-allowed data was available — all neutral).
        note: The honesty label (:data:`SCHEDULE_NOTE`).
    """

    season: int
    opponents: dict[str, dict[int, str]] = field(default_factory=dict)
    byes: dict[str, int] = field(default_factory=dict)
    multipliers: dict[str, dict[str, float]] = field(default_factory=dict)
    sos_seasons: list[int] = field(default_factory=list)
    note: str = SCHEDULE_NOTE

    def opponent(self, team: str, week: int) -> str | None:
        """The opponent ``team`` plays in ``week`` (``None`` on bye/unknown)."""
        return self.opponents.get(normalize_team(team) or team, {}).get(week)

    def bye_week(self, team: str) -> int | None:
        """A team's bye week, or ``None`` for an unknown team code."""
        return self.byes.get(normalize_team(team) or team)

    def game_weeks(self, team: str, *, through: int) -> list[int]:
        """The weeks ``team`` actually plays in ``1..through``, ascending."""
        games = self.opponents.get(normalize_team(team) or team, {})
        return sorted(w for w in games if w <= through)

    def week_multipliers(
        self, team: str, position: str, *, through: int = _REG_SEASON_WEEKS
    ) -> dict[int, float]:
        """The per-week, position-specific matchup profile for one player.

        ``{week: multiplier}`` over the weeks ``team`` actually plays in
        ``1..through`` — e.g. an RB's Week-15 entry is *that* week's opponent's
        RB-points-allowed multiplier. Bye weeks have no entry; an unknown team
        yields ``{}`` (no weekly view — never "neutral everywhere").
        """
        return {
            w: self.multiplier(position, self.opponent(team, w))
            for w in self.game_weeks(team, through=through)
        }

    def multiplier(self, position: str, defense: str | None) -> float:
        """Opponent-difficulty multiplier for ``position`` vs ``defense``.

        Higher = easier matchup (more points allowed to the position). Unknown
        position/defense (or a ``None`` defense = bye) is neutral 1.0.
        """
        if not defense:
            return 1.0
        return self.multipliers.get(normalize_position(position), {}).get(
            normalize_team(defense) or defense, 1.0
        )


@dataclass(slots=True)
class ScheduleSource:
    """Builds + caches a :class:`SeasonSchedule` from nflverse (free, no key).

    Same design rules as :class:`~fantasy_coach.ingest.projections.NflverseProjectionSource`:
    the fetch layer is injectable (tests run offline against fixtures), and the
    per-season JSON cache makes draft day zero-network after one online
    :meth:`warm_cache`.

    Args:
        nflverse: The fetch layer (``schedules`` + ``weekly_stats``).
        sos_seasons: How many completed seasons feed the points-allowed
            multipliers (1 = last season only — recent form over stale depth).
        clamp: ``(lo, hi)`` bounds on every multiplier. Tight on purpose: the
            measure is a proxy, so its influence must stay a nudge.
        scoring: Point map for the points-allowed measure (reference half-PPR;
            direction, not league precision, is what matters here).
        cache_dir: Where per-season schedule JSON caches live (git-ignored).
    """

    name: str = "nflverse_schedule"
    nflverse: NflverseSource = field(default_factory=NflverseSource)
    sos_seasons: int = 1
    clamp: tuple[float, float] = (0.75, 1.25)
    scoring: Mapping[str, float] = field(default_factory=lambda: dict(REFERENCE_SCORING))
    cache_dir: Path = field(default_factory=lambda: Path(".cache"))

    @property
    def is_live(self) -> bool:
        """True — nflverse needs no key (reachability is per-call)."""
        return True

    def load(self, season: int) -> SeasonSchedule:
        """The season's schedule — from cache when present, else computed live.

        Raises:
            RuntimeError: If no cache exists and the live nflverse pull fails
                (run :meth:`warm_cache` while online, framework §7).
        """
        cached = self._load_cache(season)
        if cached is not None:
            return cached
        try:
            schedule = self._compute(season)
        except Exception as exc:  # network / parquet / schema failures
            raise RuntimeError(
                f"nflverse schedule pull failed for season {season} and no local "
                f"cache exists at {self._cache_path(season)}. Run warm_cache() "
                "while online (pre-draft warm cache, framework §7)."
            ) from exc
        self._write_cache(schedule)
        return schedule

    def warm_cache(self, season: int) -> SeasonSchedule:
        """Fetch + compute + persist the schedule for ``season`` (pre-draft step)."""
        schedule = self._compute(season)
        self._write_cache(schedule)
        return schedule

    # -- the computation -------------------------------------------------------

    def _compute(self, season: int) -> SeasonSchedule:
        """Build the opponent map, byes, and difficulty multipliers live."""
        opponents = self._opponent_map(season)
        byes = self._byes_from(opponents)
        years = [season - offset for offset in range(1, self.sos_seasons + 1)]
        multipliers, used_years = self._points_allowed_multipliers(years)
        return SeasonSchedule(
            season=season,
            opponents=opponents,
            byes=byes,
            multipliers=multipliers,
            sos_seasons=used_years,
        )

    def _opponent_map(self, season: int) -> dict[str, dict[int, str]]:
        """``{team: {week: opponent}}`` from the season's regular-season games."""
        opponents: dict[str, dict[int, str]] = {}
        for row in _records_of(self.nflverse.schedules([season])):
            game_type = str(row.get("game_type", "REG") or "REG")
            if game_type != "REG":
                continue
            if int(_num(row, "season") or season) != season:
                continue
            week = int(_num(row, "week"))
            home = normalize_team(str(row.get("home_team", "") or ""))
            away = normalize_team(str(row.get("away_team", "") or ""))
            if not week or not home or not away:
                continue
            opponents.setdefault(home, {})[week] = away
            opponents.setdefault(away, {})[week] = home
        return opponents

    @staticmethod
    def _byes_from(opponents: dict[str, dict[int, str]]) -> dict[str, int]:
        """Each team's bye = the regular-season week it has no game.

        The week range comes from the schedule itself (weeks actually played
        league-wide), so a shortened/expanded season still derives correctly.
        A team missing several weeks (partial schedule data) records the first.
        """
        all_weeks = {w for games in opponents.values() for w in games}
        byes: dict[str, int] = {}
        for team, games in opponents.items():
            missing = sorted(all_weeks - set(games))
            if missing:
                byes[team] = missing[0]
        return byes

    def _points_allowed_multipliers(
        self, years: list[int]
    ) -> tuple[dict[str, dict[str, float]], list[int]]:
        """Per-position opponent multipliers from prior-season points allowed.

        For every REG-season weekly stat row with a known opponent, the row's
        fantasy points (reference scoring) are charged *against the defense
        that allowed them*. Per-game averages are normalized by the league
        mean at the position and clamped. Rows without an opponent column
        (older fixtures / schema drift) contribute nothing; with no usable
        rows at all the result is empty — every matchup reads neutral 1.0.
        """
        allowed: dict[str, dict[str, float]] = {p: {} for p in _SOS_POSITIONS}
        games_seen: dict[str, set[tuple[int, int]]] = {}
        used_years: set[int] = set()
        for row in _records_of(self.nflverse.weekly_stats(years)):
            if str(row.get("season_type", "REG") or "REG") != "REG":
                continue
            position = normalize_position(str(row.get("position", "") or ""))
            if position not in _SOS_POSITIONS:
                continue
            defense = normalize_team(
                str(row.get("opponent_team") or row.get("opponent") or "")
            )
            if not defense:
                continue
            year, week = int(_num(row, "season")), int(_num(row, "week"))
            used_years.add(year)
            games_seen.setdefault(defense, set()).add((year, week))
            stats = {
                key: sum(_num(row, c) for c in columns)
                for key, columns in PROJECTED_STAT_KEYS.items()
            }
            pos_allowed = allowed[position]
            pos_allowed[defense] = pos_allowed.get(defense, 0.0) + score_stats(
                stats, self.scoring
            )

        lo, hi = self.clamp
        multipliers: dict[str, dict[str, float]] = {}
        for position, by_defense in allowed.items():
            per_game = {
                team: total / len(games_seen[team])
                for team, total in by_defense.items()
                if games_seen.get(team)
            }
            if not per_game:
                continue
            mean = sum(per_game.values()) / len(per_game)
            if mean <= 0:
                continue
            multipliers[position] = {
                team: round(min(hi, max(lo, pts / mean)), 3)
                for team, pts in per_game.items()
            }
        return multipliers, sorted(used_years)

    # -- cache (framework §7 "pre-draft warm cache") --------------------------

    def _cache_path(self, season: int) -> Path:
        return self.cache_dir / f"schedule_{self.name}_{season}.json"

    def _write_cache(self, schedule: SeasonSchedule) -> None:
        """Persist the schedule as JSON, stamped with vintage + honesty note."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": self.name,
            "note": schedule.note,
            "season": schedule.season,
            "sos_seasons": schedule.sos_seasons,
            "generated_on": date.today().isoformat(),
            "opponents": {
                team: {str(week): opp for week, opp in games.items()}
                for team, games in schedule.opponents.items()
            },
            "byes": schedule.byes,
            "multipliers": schedule.multipliers,
        }
        self._cache_path(schedule.season).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _load_cache(self, season: int) -> SeasonSchedule | None:
        """Load a season's cached schedule, or ``None`` if absent/unreadable."""
        path = self._cache_path(season)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return SeasonSchedule(
                season=int(payload["season"]),
                opponents={
                    str(team): {int(w): str(opp) for w, opp in games.items()}
                    for team, games in dict(payload.get("opponents", {})).items()
                },
                byes={str(t): int(w) for t, w in dict(payload.get("byes", {})).items()},
                multipliers={
                    str(pos): {str(t): float(m) for t, m in by_team.items()}
                    for pos, by_team in dict(payload.get("multipliers", {})).items()
                },
                sos_seasons=[int(y) for y in payload.get("sos_seasons", [])],
                note=str(payload.get("note", SCHEDULE_NOTE)),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None  # corrupt cache -> treat as absent, recompute live
