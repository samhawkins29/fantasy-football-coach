"""``YahooClient`` — a clean, typed read client over the Yahoo Fantasy API (M2).

This wraps the M1 :class:`~fantasy_coach.auth.session.AuthedClient` (which owns
bearer injection + transparent token refresh — framework §2.4) and turns Yahoo's
REST surface into typed models. It deliberately does **not** delegate to a
community wrapper (``yfpy``/``yahoo_fantasy_api``): we own the parse layer so the
whole thing stays offline-testable via an injected ``httpx.MockTransport`` and so
a wrapper breaking the week before the draft can't block us (framework §2.4 "keep
a raw ``httpx`` client that can hit any endpoint directly").

Responsibilities:

* Build Yahoo's matrix-selector paths (``players;start=0;count=25;status=A``) and
  always request ``?format=json`` (framework §2.3).
* Route every call through a conservative :class:`~fantasy_coach.clients.throttle.Throttle`
  (framework §7) and apply exponential backoff on ``429``/``999`` throttles.
* Parse each response into models via :mod:`fantasy_coach.clients.parsers`,
  handling the 25-per-page player pagination.
* Expose :meth:`raw` as an escape hatch returning the parsed-but-unmodelled JSON.

**No live Yahoo data flows until the user authenticates via M1** — this client
just needs an :class:`AuthedClient`; provide one built from a real (authenticated)
:class:`~fantasy_coach.config.Config`, or an injected one for tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence

import httpx

from fantasy_coach.auth.session import YAHOO_API_BASE, AuthedClient
from fantasy_coach.clients import parsers
from fantasy_coach.clients.models import (
    DraftPick,
    Game,
    League,
    LeagueSettings,
    Matchup,
    Player,
    Team,
    TeamRoster,
    Transaction,
)
from fantasy_coach.clients.throttle import DRAFT_POLL_INTERVAL, Throttle

# HTTP statuses we treat as "Yahoo throttled us, back off and retry" (§7).
# 429 = standard rate limit; 999 = Yahoo's historical throttle sentinel.
RETRYABLE_STATUS = frozenset({429, 999})

# Yahoo returns players 25 at a time; pagination walks ``start`` by this step.
PLAYER_PAGE_SIZE = 25

# Default warm-cache lifetime. Framework §7: pull all static data (settings,
# players, teams) *before* the draft and compute locally, so during the draft
# the only live call is draftresults. 15 minutes is long enough to make a
# pre-draft warm-up cheap and short enough that in-season data stays fresh.
DEFAULT_CACHE_TTL = 900.0


class _TTLCache:
    """A tiny time-to-live cache for GET responses.

    Not an LRU: the working set is a few dozen Yahoo endpoints per league, so
    entries are only ever evicted by expiry. The clock is injectable so tests
    can exercise expiry without sleeping.
    """

    def __init__(self, time_func: Callable[[], float] = time.monotonic) -> None:
        self._time = time_func
        self._entries: dict[str, tuple[float, dict]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict | None:
        """Return the cached payload for ``key``, or ``None`` if absent/expired."""
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, payload = entry
        if self._time() >= expires_at:
            del self._entries[key]
            self.misses += 1
            return None
        self.hits += 1
        return payload

    def put(self, key: str, payload: dict, ttl: float) -> None:
        """Cache ``payload`` under ``key`` for ``ttl`` seconds (no-op if ``ttl<=0``)."""
        if ttl > 0:
            self._entries[key] = (self._time() + ttl, payload)

    def clear(self) -> None:
        """Drop every cached entry."""
        self._entries.clear()

    @property
    def stats(self) -> dict[str, int]:
        """Diagnostics: hit/miss counts and current entry count."""
        return {"hits": self.hits, "misses": self.misses, "entries": len(self._entries)}


class YahooAPIError(RuntimeError):
    """A non-success, non-retryable response from the Yahoo Fantasy API.

    Attributes:
        status_code: The HTTP status returned.
        url: The request URL (for debugging).
    """

    def __init__(self, status_code: int, url: str, message: str = "") -> None:
        self.status_code = status_code
        self.url = url
        detail = f" — {message}" if message else ""
        super().__init__(f"Yahoo API returned {status_code} for {url}{detail}")


class YahooClient:
    """Typed read access to a user's Yahoo Fantasy data.

    Args:
        authed: An authenticated client from M1 (``get_authed_client(config)``).
        throttle: Rate limiter shared across calls. Defaults to one request
            every :data:`~fantasy_coach.clients.throttle.DRAFT_POLL_INTERVAL`
            seconds (2.5s) — the framework §7 live-draft-safe rate, so a draft
            poll loop is safe with the *default* client. Bulk pre-draft warming
            can pass a faster ``Throttle(DEFAULT_MIN_INTERVAL)``.
        cache_ttl: Warm-cache lifetime in seconds for read endpoints. ``0``
            disables caching. ``get_draft_results`` always bypasses it.
        game_key: Default game key/alias for discovery. ``"nfl"`` resolves to the
            current in-season NFL game (framework §2.3).
        max_retries: Attempts (including the first) on ``429``/``999`` before
            giving up with a :class:`YahooAPIError`.
        base_url: API base; defaults to the M1 constant. Overridable for tests.
        time_func: Monotonic clock for the cache. Injectable for tests.
    """

    def __init__(
        self,
        authed: AuthedClient,
        *,
        throttle: Throttle | None = None,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        game_key: str = "nfl",
        max_retries: int = 4,
        base_url: str = YAHOO_API_BASE,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self._authed = authed
        self._throttle = (
            throttle if throttle is not None else Throttle(DRAFT_POLL_INTERVAL)
        )
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._cache = _TTLCache(time_func)
        self._game_key = game_key
        self._max_retries = max(1, max_retries)
        self._base_url = base_url.rstrip("/")

    # -- lifecycle -----------------------------------------------------------

    @property
    def throttle(self) -> Throttle:
        """The rate limiter this client routes requests through."""
        return self._throttle

    @property
    def cache(self) -> _TTLCache:
        """The warm cache backing read endpoints (exposed for diagnostics)."""
        return self._cache

    def clear_cache(self) -> None:
        """Drop every cached response — e.g. after league settings change."""
        self._cache.clear()

    def close(self) -> None:
        """Close the underlying authenticated client."""
        self._authed.close()

    def __enter__(self) -> "YahooClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low-level request path ---------------------------------------------

    def _url(self, path: str) -> str:
        """Absolute URL for a Yahoo resource path (no leading slash needed)."""
        return f"{self._base_url}/{path.lstrip('/')}"

    def raw(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        cache_ttl: float | None = None,
    ) -> dict:
        """Fetch ``path`` and return the parsed JSON dict (the escape hatch).

        Serves from the warm cache when possible; otherwise applies the
        throttle, appends ``?format=json``, and retries on Yahoo throttle
        responses. Performs **no** modelling — use for endpoints this client
        doesn't yet wrap, or to inspect Yahoo's raw shape.

        Args:
            path: Resource path relative to the API base (matrix selectors and
                sub-resource chaining with ``/`` are the caller's responsibility).
            params: Extra query parameters (``format=json`` is always added).
            cache_ttl: Override the client's default cache lifetime for this
                call. Pass ``0`` to force a live fetch and skip caching — which
                is what live-draft polling does.

        Raises:
            YahooAPIError: On a non-2xx, non-retryable response (or exhausted
                retries).
        """
        url = self._url(path)
        query = {"format": "json"}
        if params:
            query.update(params)

        ttl = self._cache_ttl if cache_ttl is None else max(0.0, float(cache_ttl))
        cache_key = f"{url}?{sorted(query.items())}"
        if ttl > 0:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        resp = self._request_with_retry(url, query)
        if resp.status_code >= 400 or resp.status_code in RETRYABLE_STATUS:
            raise YahooAPIError(resp.status_code, url, _error_snippet(resp))
        try:
            payload = resp.json()
        except ValueError as exc:  # not JSON — Yahoo occasionally returns XML/HTML
            raise YahooAPIError(resp.status_code, url, f"non-JSON body: {exc}") from exc

        self._cache.put(cache_key, payload, ttl)
        return payload

    def _request_with_retry(self, url: str, query: dict) -> httpx.Response:
        """Issue a GET, throttling each attempt and backing off on throttles."""
        resp: httpx.Response | None = None
        for attempt in range(1, self._max_retries + 1):
            self._throttle.wait()
            resp = self._authed.get(url, params=query)
            if resp.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                self._throttle.backoff_sleep(attempt)
                continue
            break
        assert resp is not None  # loop always runs at least once
        return resp

    # -- game / league discovery --------------------------------------------

    def get_game(self, game_key: str | None = None) -> Game:
        """Fetch ``/game/{key}`` → :class:`Game` (defaults to ``nfl`` alias)."""
        key = game_key or self._game_key
        return parsers.parse_game(self.raw(f"game/{key}"))

    def get_game_key(self, game_key: str | None = None) -> str:
        """Resolve the current season's numeric ``game_key`` (e.g. ``"449"``).

        The ``nfl`` alias resolves server-side to the in-season game; this reads
        it back so callers can build ``{game_key}.l.{id}`` keys. Framework §2.3
        flags the exact 2026 value as ``[verify at setup]`` — we read it live.
        """
        return self.get_game(game_key).game_key

    def get_user_leagues(
        self,
        season: str | int | None = None,
        *,
        game_key: str | None = None,
        game_code: str = "nfl",
    ) -> list[League]:
        """Discover the authenticated user's leagues (framework §2.3, §6.2).

        **Game-key handling.** Yahoo scopes leagues under a *game*, and the game
        key changes every season (``449`` for one year, something else the next
        — §2.3 flags the 2026 value as ``[verify at setup]``). Rather than make
        the caller know it:

        * with no ``season``, we use the ``nfl`` alias, which Yahoo resolves
          server-side to the current in-season game;
        * with a ``season``, we ask by ``game_codes=nfl;seasons={season}``, so
          Yahoo hands back that season's game — a past season is reachable
          without ever hard-coding a numeric key.

        Either way each returned :class:`League` carries the resolved
        ``game_key`` and ``season`` inherited from its parent game node, so
        ``league.game_key`` is always the right prefix for building team and
        player keys.

        Args:
            season: Optional 4-digit season (e.g. ``2026``). Omit for current.
            game_key: Explicit game key/alias, overriding ``season`` handling.
            game_code: Sport code to discover under (defaults to ``"nfl"``).

        Returns:
            Every :class:`League` the user belongs to for the resolved game.
        """
        if game_key is not None:
            games_selector = f"games;game_keys={game_key}"
        elif season is not None:
            games_selector = f"games;game_codes={game_code};seasons={int(season)}"
        else:
            games_selector = f"games;game_keys={self._game_key}"

        path = f"users;use_login=1/{games_selector}/leagues"
        leagues = parsers.parse_user_leagues(self.raw(path))
        if season is not None:
            # Belt-and-braces: Yahoo occasionally ignores the seasons filter and
            # returns every game the user has ever played.
            leagues = [lg for lg in leagues if lg.season == str(season)]
        return leagues

    def get_league(self, league_key: str) -> League:
        """Fetch ``/league/{league_key}`` metadata → :class:`League`."""
        return parsers.parse_league(self.raw(f"league/{league_key}"))

    # -- settings ------------------------------------------------------------

    def get_league_settings(self, league_key: str) -> LeagueSettings:
        """Fetch ``/league/{key}/settings`` → :class:`LeagueSettings`.

        Carries the scoring rules, roster slots, and waiver/FAAB/playoff config
        that M3 turns into a ``ScoringProfile`` (framework §3.4).
        """
        return parsers.parse_league_settings(
            self.raw(f"league/{league_key}/settings")
        )

    # -- teams + rosters -----------------------------------------------------

    def get_league_teams(self, league_key: str) -> list[Team]:
        """Fetch ``/league/{key}/teams`` → every :class:`Team`."""
        return parsers.parse_league_teams(self.raw(f"league/{league_key}/teams"))

    def get_team_roster(
        self, team_key: str, *, week: int | None = None
    ) -> TeamRoster:
        """Fetch ``/team/{key}/roster`` (optionally for ``week``) → a roster.

        Args:
            team_key: ``{league_key}.t.{team_id}``.
            week: NFL week to read the lineup for; omit for the current week.

        Returns:
            A :class:`TeamRoster` whose ``slots`` pair each player with the
            lineup position they occupy (``starters``/``bench`` split included).
        """
        path = f"team/{team_key}/roster"
        if week is not None:
            path += f";week={int(week)}"
        return parsers.parse_team_roster(self.raw(path))

    # -- players (paginated) -------------------------------------------------

    def get_players(
        self,
        league_key: str,
        *,
        status: str | None = None,
        position: str | None = None,
        sort: str | None = None,
        search: str | None = None,
        player_keys: Sequence[str] | None = None,
        out: Sequence[str] | None = None,
        sub_resource: str | None = None,
        start: int = 0,
        page_size: int = PLAYER_PAGE_SIZE,
        max_players: int | None = None,
    ) -> list[Player]:
        """Fetch the player universe, paginating Yahoo's 25-per-page limit.

        Loops ``start=0,25,50,…`` until a short or empty page, or until
        ``max_players`` is reached (framework §2.3). Filters map to Yahoo matrix
        selectors. A short page is the *only* reliable end-of-data signal —
        Yahoo's ``count`` field is unreliable on the final page — so the loop
        keys on the number of parsed players, never on ``count``.

        Args:
            league_key: The league whose player pool to read.
            status: ``A`` (available), ``FA``, ``W`` (waivers), ``T`` (taken)…
            position: Filter to a position (``RB``, ``WR``, …).
            sort: Yahoo sort key (e.g. ``AR`` actual rank, ``OR`` overall rank,
                ``PTS``, or a ``stat_id``).
            search: Name search fragment.
            player_keys: Fetch these specific players (disables pagination).
            out: Sub-resources to inline on each player via Yahoo's ``out=``
                selector — e.g. ``("percent_owned", "draft_analysis",
                "ranks")`` to get ownership, ADP, and Yahoo's own ranks in the
                same request instead of one round-trip per player.
            sub_resource: Chain a single sub-resource with ``/`` instead
                (``"percent_owned"``). Prefer ``out`` for more than one.
            start: Starting offset (usually 0).
            page_size: Results per page (Yahoo caps at 25).
            max_players: Stop once this many are collected (caps total requests).

        Returns:
            A flat list of :class:`Player` across all pages.
        """
        collected: list[Player] = []
        cursor = start
        specific = bool(player_keys)

        while True:
            selectors = ["players"]
            if status:
                selectors.append(f"status={status}")
            if position:
                selectors.append(f"position={position}")
            if sort:
                selectors.append(f"sort={sort}")
            if search:
                selectors.append(f"search={search}")
            if player_keys:
                selectors.append("player_keys=" + ",".join(player_keys))
            if out:
                selectors.append("out=" + ",".join(out))
            selectors.append(f"start={cursor}")
            selectors.append(f"count={page_size}")

            path = f"league/{league_key}/" + ";".join(selectors)
            if sub_resource:
                path += f"/{sub_resource}"

            page = parsers.parse_players(self.raw(path))
            collected.extend(page)

            if max_players is not None and len(collected) >= max_players:
                return collected[:max_players]
            # Stop on a short page (last page) or when fetching specific keys.
            if specific or len(page) < page_size:
                break
            cursor += page_size

        return collected

    # -- draft ---------------------------------------------------------------

    def get_draft_results(self, league_key: str) -> list[DraftPick]:
        """Fetch ``/league/{key}/draftresults`` → ordered :class:`DraftPick`s.

        This is the one endpoint M5 polls *hot* during a live draft (framework
        §7), so it **always bypasses the warm cache** — a cached draft board is
        worse than useless mid-round. The throttle still applies, which is what
        keeps the poll loop at a Yahoo-safe rate.

        During a live draft Yahoo returns the full pick list up front with
        unmade picks blank; filter on :attr:`DraftPick.is_made` and diff on the
        made-pick count rather than the list length (§7 "diff, don't re-pull").
        """
        return parsers.parse_draft_results(
            self.raw(f"league/{league_key}/draftresults", cache_ttl=0)
        )

    # -- transactions --------------------------------------------------------

    def get_transactions(
        self,
        league_key: str,
        *,
        types: Sequence[str] | None = None,
        count: int | None = None,
    ) -> list[Transaction]:
        """Fetch ``/league/{key}/transactions`` → :class:`Transaction`s.

        Args:
            league_key: The league to read.
            types: Filter to transaction types (``add``, ``drop``, ``trade``,
                ``commish``, ``waiver``).
            count: Cap the number of transactions returned.
        """
        selectors = ["transactions"]
        if types:
            selectors.append("types=" + ",".join(types))
        if count is not None:
            selectors.append(f"count={int(count)}")
        path = f"league/{league_key}/" + ";".join(selectors)
        return parsers.parse_transactions(self.raw(path))

    # -- matchups / scoreboard ----------------------------------------------

    def get_scoreboard(
        self, league_key: str, *, week: int | None = None
    ) -> list[Matchup]:
        """Fetch ``/league/{key}/scoreboard`` (optionally ``;week=N``) → matchups."""
        path = f"league/{league_key}/scoreboard"
        if week is not None:
            path += f";week={int(week)}"
        return parsers.parse_scoreboard(self.raw(path))

    def get_matchups(self, league_key: str, week: int | None = None) -> list[Matchup]:
        """Fetch one week's matchups → :class:`Matchup` records.

        Matchups live under the scoreboard resource in Yahoo's model, so this
        is a thin alias for :meth:`get_scoreboard` with ``week`` accepted
        positionally — the shape M11's projected-vs-actual calibration (§4.5)
        iterates week by week.

        Args:
            league_key: The league to read.
            week: NFL week; omit for the league's current week.
        """
        return self.get_scoreboard(league_key, week=week)


def _error_snippet(resp: httpx.Response) -> str:
    """A short, safe snippet of an error body for diagnostics."""
    try:
        text = resp.text
    except (UnicodeDecodeError, httpx.ResponseNotRead):
        return ""
    text = text.strip().replace("\n", " ")
    return text[:200]


__all__ = [
    "YahooClient",
    "YahooAPIError",
    "RETRYABLE_STATUS",
    "PLAYER_PAGE_SIZE",
]
