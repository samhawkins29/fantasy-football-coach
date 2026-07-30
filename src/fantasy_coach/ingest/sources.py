"""External data feeds behind a common interface (framework §2.5).

The framework's "all the data" layer: every source — free or paid, live or
stubbed — implements one of two protocols so the value engine (M4) and the
calibration loop (M11) can *blend and reweight* them without caring where a
number came from (§4.1, §4.5).

* :class:`DataSource` — a generic feed with a ``name`` and a liveness flag.
* :class:`ProjectionSource` — a source that yields per-player projected points
  (FantasyPros, premium providers). Its ``project()`` returns points keyed by a
  *source-native* id; the caller resolves those to canonical ids via the
  :class:`~fantasy_coach.ingest.resolver.IdResolver`.

Implemented **live** (free, no key — the backbone):

* :class:`NflverseSource` — nflverse via ``nfl_data_py`` (id map, weekly stats,
  snap counts, depth charts, injuries, schedules). Every ``nfl_data_py`` call is
  lazily imported *inside* the method and the fetchers are injectable, so this
  module imports with no pandas/nfl_data_py dependency and every test runs
  offline against fixtures.
* :class:`SleeperSource` — the Sleeper API (players blob w/ cross-platform ids,
  trending adds/drops, ADP-ish, injury status). Built on an injected
  ``httpx.Client``, so it is offline-testable with ``httpx.MockTransport`` — the
  exact pattern M2 uses.

Wired **behind the interface but key-gated / stubbed** (turn on later without
touching the engine):

* :class:`FantasyProsSource` — projections/ECR/ADP (partner API key).
* :class:`OddsApiSource` — Vegas totals/spreads → implied team totals (free-tier
  key).
* :class:`OpenMeteoSource` — stadium weather (no key; join wired, fetch stubbed).

Each stub implements its protocol and reports ``is_live == False`` until
configured, and raises :class:`SourceNotConfigured` if you actually call it
without credentials — so a caller can iterate every registered source and skip
the ones that aren't ready.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from fantasy_coach.ingest.names import normalize_team

__all__ = [
    "DataSource",
    "ProjectionSource",
    "SourceNotConfigured",
    "ProjectionRecord",
    "TrendingPlayer",
    "ImpliedTotal",
    "WeatherReport",
    "NflverseSource",
    "SleeperSource",
    "FantasyProsSource",
    "OddsApiSource",
    "OpenMeteoSource",
    "SLEEPER_BASE_URL",
]

SLEEPER_BASE_URL = "https://api.sleeper.app/v1"


class SourceNotConfigured(RuntimeError):
    """Raised when a key-gated source is used without its credentials/config."""


@runtime_checkable
class DataSource(Protocol):
    """A named external feed. The minimal contract every source satisfies."""

    name: str

    @property
    def is_live(self) -> bool:
        """True when the source can actually fetch (configured + reachable path)."""
        ...


# --------------------------------------------------------------------------- #
# Normalized structures the sources emit (join targets for the PlayerIndex)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ProjectionRecord:
    """One source's projection for one player, in that source's id namespace.

    ``source_id_field`` names which :class:`ExternalIds` column ``source_id``
    lives in (``"fantasypros_id"``, ``"sleeper_id"`` …) so the caller can route
    it through the crosswalk. ``points`` is the source's own number *before* M4
    rescores it into the league's scoring (M4 §4.1 prefers rescoring raw stats,
    but a pre-scored consensus is still a useful blend input).
    """

    source: str
    source_id: str
    source_id_field: str
    points: float
    floor: float | None = None
    ceiling: float | None = None
    position: str = ""
    team: str = ""
    name: str = ""


@dataclass(slots=True)
class TrendingPlayer:
    """A Sleeper trending add/drop signal — a waiver/FAAB input (M7/M9)."""

    sleeper_id: str
    count: int
    direction: str  # "add" or "drop"


@dataclass(slots=True)
class ImpliedTotal:
    """A team's Vegas-implied point total for a game (M4 §4.1 step 4).

    ``implied_team_total = over_under/2 ± spread/2`` (framework §2.5). A higher
    implied total scales a player's projection up.
    """

    team: str
    implied_total: float
    spread: float
    over_under: float
    opponent: str = ""
    kickoff: str = ""


@dataclass(slots=True)
class WeatherReport:
    """Stadium weather for a game (M4 §4.1 step 4). Domes are neutral."""

    team: str
    wind_mph: float | None = None
    precip_pct: float | None = None
    temp_f: float | None = None
    is_dome: bool = False


@runtime_checkable
class ProjectionSource(DataSource, Protocol):
    """A :class:`DataSource` that yields per-player projected points (§4.1)."""

    def project(self, *, week: int | None = None, season: int | None = None) -> list[ProjectionRecord]:
        """Return projections; ``week=None`` means rest-of-season/seasonal."""
        ...


# --------------------------------------------------------------------------- #
# nflverse (live backbone) — framework §2.5 "The backbone"
# --------------------------------------------------------------------------- #


class NflverseSource:
    """nflverse data via ``nfl_data_py`` (the free, no-key backbone).

    Wraps the ``nfl_data_py`` functions the app needs (§2.5): the id map, weekly
    player stats, snap counts, depth charts, injuries, and schedules. Two design
    rules keep it offline-testable (the M1/M2 requirement):

    * **Lazy import.** ``nfl_data_py`` is imported *inside* each method, never at
      module load — so importing this module (and running the whole test suite)
      needs neither pandas nor nfl_data_py.
    * **Injectable fetchers.** Pass a ``fetchers`` mapping of
      ``{"weekly": fn, "ids": fn, ...}`` to replace the real functions with
      fixtures/mocks. Tests always inject; production leaves it ``None`` and the
      real ``nfl_data_py`` functions are bound on first use.

    Each accessor returns whatever the underlying function returns (a
    ``pandas.DataFrame`` in production, or the injected fixture in tests) — the
    :class:`~fantasy_coach.ingest.index.PlayerIndex` is responsible for the
    normalize+join step (§3.2), not this thin fetch wrapper.
    """

    name = "nflverse"

    #: Logical name -> ``nfl_data_py`` attribute, resolved lazily.
    _FUNCTIONS = {
        "ids": "import_ids",
        "weekly": "import_weekly_data",
        "snaps": "import_snap_counts",
        "depth_charts": "import_depth_charts",
        "injuries": "import_injuries",
        "schedules": "import_schedules",
        "seasonal_rosters": "import_seasonal_rosters",
    }

    def __init__(self, fetchers: Mapping[str, Callable[..., object]] | None = None) -> None:
        self._fetchers = dict(fetchers or {})

    @property
    def is_live(self) -> bool:
        """True — nflverse needs no key. (Actual reachability is per-call.)"""
        return True

    def _fetch(self, logical: str, *args: object, **kwargs: object) -> object:
        """Call an injected fetcher, or lazily bind the real ``nfl_data_py`` one."""
        fn = self._fetchers.get(logical)
        if fn is None:
            import nfl_data_py  # noqa: PLC0415  (intentional lazy import)

            attr = self._FUNCTIONS[logical]
            fn = getattr(nfl_data_py, attr)
        return fn(*args, **kwargs)

    # Each of these is deliberately a thin pass-through (the join lives in
    # PlayerIndex); they exist to name the surface and centralize the lazy call.

    def id_map(self) -> object:
        """The DynastyProcess player-id crosswalk (``import_ids``)."""
        return self._fetch("ids")

    def weekly_stats(self, years: Sequence[int]) -> object:
        """Per-player weekly box scores (``import_weekly_data``)."""
        return self._fetch("weekly", list(years))

    def snap_counts(self, years: Sequence[int]) -> object:
        """Offensive/defensive snap counts (``import_snap_counts``)."""
        return self._fetch("snaps", list(years))

    def depth_charts(self, years: Sequence[int]) -> object:
        """Weekly depth charts (``import_depth_charts``)."""
        return self._fetch("depth_charts", list(years))

    def injuries(self, years: Sequence[int]) -> object:
        """Injury reports / practice status (``import_injuries``)."""
        return self._fetch("injuries", list(years))

    def schedules(self, years: Sequence[int]) -> object:
        """Game schedules incl. bye derivation (``import_schedules``)."""
        return self._fetch("schedules", list(years))


# --------------------------------------------------------------------------- #
# Sleeper (live, no key) — framework §2.5 "Rankings/news/ADP + IDs"
# --------------------------------------------------------------------------- #


class SleeperSource:
    """The Sleeper API: player blob (w/ ``yahoo_id`` etc.), trending, injuries.

    Free and keyless (§2.5). Built on an injected ``httpx.Client`` so it is
    offline-testable with ``httpx.MockTransport`` — the same seam M2's
    ``AuthedClient`` uses. The client owner is responsible for politeness; the
    framework note is "cache the big players blob daily" (§2.5, §7).

    Args:
        client: An ``httpx.Client`` (real, or one wired to a ``MockTransport``).
            If ``None``, a plain client is created lazily on first request.
        base_url: Sleeper API base (overridable for tests).
        timeout: Per-request timeout in seconds.
    """

    name = "sleeper"

    def __init__(
        self,
        client: object | None = None,
        *,
        base_url: str = SLEEPER_BASE_URL,
        timeout: float = 20.0,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @property
    def is_live(self) -> bool:
        """True — Sleeper needs no credentials."""
        return True

    def _get_client(self) -> object:
        """Return the httpx client, lazily creating a default one if needed."""
        if self._client is None:
            import httpx  # noqa: PLC0415

            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _get_json(self, path: str) -> object:
        """GET ``{base}/{path}`` and return parsed JSON."""
        client = self._get_client()
        resp = client.get(f"{self._base_url}/{path.lstrip('/')}")
        resp.raise_for_status()
        return resp.json()

    def players(self) -> dict[str, dict]:
        """Fetch ``GET /players/nfl`` → ``{sleeper_id: player_obj}``.

        The player objects carry ``yahoo_id``/``gsis_id``/``espn_id``/… — the
        crosswalk's Sleeper gap-fill source (§3.1). This is a large blob; the
        caller should cache it daily.
        """
        data = self._get_json("players/nfl")
        return data if isinstance(data, dict) else {}

    def trending(self, direction: str = "add", *, limit: int = 25, lookback_hours: int = 24) -> list[TrendingPlayer]:
        """Fetch trending adds/drops → :class:`TrendingPlayer` list (waiver signal).

        Args:
            direction: ``"add"`` or ``"drop"``.
            limit: Max players to return.
            lookback_hours: Sleeper's ``lookback_hours`` window.
        """
        path = f"players/nfl/trending/{direction}?lookback_hours={lookback_hours}&limit={limit}"
        data = self._get_json(path)
        out: list[TrendingPlayer] = []
        if isinstance(data, list):
            for row in data:
                if isinstance(row, Mapping) and row.get("player_id") is not None:
                    out.append(
                        TrendingPlayer(
                            sleeper_id=str(row["player_id"]),
                            count=int(row.get("count", 0) or 0),
                            direction=direction,
                        )
                    )
        return out

    def close(self) -> None:
        """Close the underlying client if we own it."""
        client = self._client
        if client is not None and hasattr(client, "close"):
            client.close()  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Key-gated / stubbed sources (wired behind the interface, off until configured)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class FantasyProsSource:
    """FantasyPros consensus projections / ECR / ADP (partner API — §2.5).

    Wired to the :class:`ProjectionSource` protocol but **off** until an API key
    is provided (the free path is a low-volume personal scrape the framework
    flags as a ToS gray area, so we don't ship it). With no key, ``is_live`` is
    ``False`` and :meth:`project` raises :class:`SourceNotConfigured` — callers
    iterating sources skip it cleanly.
    """

    name: str = "fantasypros"
    api_key: str = ""
    _fetch: Callable[..., list[ProjectionRecord]] | None = field(default=None, repr=False)

    @property
    def is_live(self) -> bool:
        """True only when an API key (or an injected fetcher) is present."""
        return bool(self.api_key) or self._fetch is not None

    def project(self, *, week: int | None = None, season: int | None = None) -> list[ProjectionRecord]:
        """Return FantasyPros projections, or raise if unconfigured."""
        if self._fetch is not None:
            return self._fetch(week=week, season=season)
        raise SourceNotConfigured(
            "FantasyPros needs an API key (set FANTASYPROS_API_KEY). "
            "Projections route via 'fantasypros_id' through the crosswalk."
        )


@dataclass(slots=True)
class OddsApiSource:
    """The Odds API → Vegas implied team totals (free-tier key — §2.5).

    Derives ``implied_team_total = over_under/2 ± spread/2`` (framework §2.5).
    Off until ``ODDS_API_KEY`` is set; an injected ``fetch`` lets tests exercise
    the derivation offline.
    """

    name: str = "odds_api"
    api_key: str = ""
    _fetch: Callable[..., list[Mapping[str, object]]] | None = field(default=None, repr=False)

    @property
    def is_live(self) -> bool:
        """True only when a key (or injected fetcher) is present."""
        return bool(self.api_key) or self._fetch is not None

    @staticmethod
    def implied_totals(games: Sequence[Mapping[str, object]]) -> list[ImpliedTotal]:
        """Derive per-team implied totals from ``{team, opponent, spread, total}`` rows.

        ``implied = total/2 - spread/2`` for the *favorite* (negative spread
        gives it the larger share). Pure math, so it is unit-tested without a key.
        """
        out: list[ImpliedTotal] = []
        for g in games:
            total = float(g.get("over_under", g.get("total", 0.0)) or 0.0)
            spread = float(g.get("spread", 0.0) or 0.0)
            team = normalize_team(str(g.get("team", ""))) or str(g.get("team", ""))
            opp = normalize_team(str(g.get("opponent", ""))) or str(g.get("opponent", ""))
            implied = total / 2.0 - spread / 2.0
            out.append(
                ImpliedTotal(
                    team=team,
                    implied_total=round(implied, 2),
                    spread=spread,
                    over_under=total,
                    opponent=opp,
                    kickoff=str(g.get("kickoff", "")),
                )
            )
        return out

    def fetch_implied_totals(self, *, week: int | None = None) -> list[ImpliedTotal]:
        """Fetch live odds and derive implied totals, or raise if unconfigured."""
        if self._fetch is not None:
            return self.implied_totals(self._fetch(week=week))
        raise SourceNotConfigured(
            "The Odds API needs a key (set ODDS_API_KEY). Free tier ~500 req/mo."
        )


@dataclass(slots=True)
class OpenMeteoSource:
    """Open-Meteo stadium weather (no key; §2.5). Domes are neutral.

    The fetch is stubbed (wired behind the interface) because it needs a
    stadium→lat/lon table we build later; the ``is_dome`` neutralization and the
    :class:`WeatherReport` shape are defined now so M4's environment adjustment
    has a stable target. Inject ``fetch`` to exercise the path offline.
    """

    name: str = "open_meteo"
    _fetch: Callable[..., list[WeatherReport]] | None = field(default=None, repr=False)
    #: Teams that play in a dome / fixed roof — weather is neutral for these.
    dome_teams: frozenset[str] = field(
        default_factory=lambda: frozenset({"ATL", "DET", "MIN", "NO", "LV", "LAR", "ARI", "DAL", "HOU", "IND"})
    )

    @property
    def is_live(self) -> bool:
        """True only when a fetcher is injected (no default lat/lon table yet)."""
        return self._fetch is not None

    def is_dome(self, team: str) -> bool:
        """True when a team's home is a dome/fixed roof (weather neutral)."""
        return (normalize_team(team) or team) in self.dome_teams

    def weather_for(self, teams: Sequence[str], *, when: str | None = None) -> list[WeatherReport]:
        """Return weather per team, or raise if no fetcher is wired.

        Dome teams short-circuit to a neutral report without a fetch.
        """
        if self._fetch is None:
            raise SourceNotConfigured(
                "Open-Meteo weather needs the stadium lat/lon table (built later) "
                "or an injected fetcher. Dome teams are neutral without it."
            )
        return self._fetch(teams=teams, when=when)
