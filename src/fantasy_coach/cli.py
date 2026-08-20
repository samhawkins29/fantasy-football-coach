"""Command-line interface for Fantasy Coach.

Commands:
    login    Run the Yahoo OAuth consent flow and store tokens.
    status   Show the stored token's state (a.k.a. whoami). No network.
    logout   Delete the stored tokens.
    config   Show which config values are set (secrets masked). No network.
    draft    The live draft companion (M5) — poll the draft room, serve the
             recommendation board at http://localhost:<port>. ``--simulate``
             replays a scripted draft offline through the identical loop.
    refresh  Pull the latest of everything against the most up-to-date sources
             (projections, schedule, durability, Sleeper + Yahoo injury
             status, ADP), rebuild the board, and report data vintage — run
             it right before the draft.
    setup-league  Load your league's exact rules from the offline spec
             (``data/league.json``: scoring, roster incl. IDP / no-K,
             playoffs, draft length, keepers), store them, and build the
             board from the local caches — no Yahoo needed.
    vintage  Show how fresh every stored data slice is. No network.

Run ``python -m fantasy_coach --help`` for usage.

The ``login`` flow is deliberately manual (print URL, paste redirect) — M1 runs
no local callback server and makes no live calls except the single, explicit
token exchange the user initiates by pasting a real code.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import typer
from rich.console import Console
from rich.table import Table

from fantasy_coach.auth import TokenStore, YahooOAuthClient
from fantasy_coach.auth.session import AuthedClient, DEFAULT_REFRESH_LEEWAY
from fantasy_coach.config import Config, ConfigError

app = typer.Typer(
    name="fantasy-coach",
    help="Fantasy Coach — Yahoo auth (M1) + the live draft companion (M5).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _load_config() -> Config:
    """Load config or exit with a friendly message if OAuth creds are missing."""
    config = Config.load()
    try:
        config.require_oauth()
    except ConfigError as exc:
        console.print(f"[bold red]Configuration error:[/] {exc}")
        raise typer.Exit(code=1)
    return config


def _extract_code(user_input: str) -> str:
    """Pull the auth code out of either a full redirect URL or a bare code.

    Accepts the whole ``https://localhost:8000/callback?code=...&state=...``
    URL (easiest for the user to paste) or just the code itself.
    """
    text = user_input.strip()
    if "code=" in text and ("://" in text or text.startswith("?") or "&" in text):
        query = urlparse(text).query or text.lstrip("?")
        params = parse_qs(query)
        codes = params.get("code")
        if codes:
            return codes[0]
    return text


@app.command()
def login(
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Don't try to open a browser automatically."
    ),
) -> None:
    """Run the Yahoo OAuth consent flow and store the resulting tokens.

    Prints the consent URL, waits for you to authorize in a browser, then
    accepts the redirected URL (or bare code) and exchanges it for tokens.
    """
    config = _load_config()
    oauth = YahooOAuthClient(
        client_id=config.yahoo_client_id,
        client_secret=config.yahoo_client_secret,
        redirect_uri=config.yahoo_redirect_uri,
        scope=config.yahoo_scope,
    )
    store = TokenStore(config.token_path)

    auth_url, state = oauth.create_authorization_url()

    console.print("\n[bold]Step 1.[/] Open this URL and authorize the app:\n")
    console.print(f"  [cyan]{auth_url}[/]\n")
    console.print(
        f"[dim]After you approve, Yahoo redirects to "
        f"{config.yahoo_redirect_uri} with ?code=... (the page may fail to "
        f"load — that's fine, it's a local URL).[/]\n"
    )

    if not no_browser:
        try:
            import webbrowser

            webbrowser.open(auth_url)
        except Exception:  # pragma: no cover - environment dependent
            pass

    console.print("[bold]Step 2.[/] Paste the FULL redirected URL (or just the code):")
    pasted = typer.prompt("  redirect URL / code")
    returned = _extract_code(pasted)

    # Best-effort CSRF check when the user pasted a full URL containing state.
    returned_state = _extract_state(pasted)
    if returned_state and returned_state != state:
        console.print(
            "[bold red]Warning:[/] state mismatch — the redirect's state does "
            "not match what we sent. Aborting for safety."
        )
        raise typer.Exit(code=1)

    try:
        token = oauth.fetch_token(returned)
    except Exception as exc:
        console.print(f"[bold red]Token exchange failed:[/] {exc}")
        raise typer.Exit(code=1)
    finally:
        oauth.close()

    store.save(token)
    console.print(
        f"\n[bold green]Success.[/] Tokens saved to [cyan]{store.path}[/]. "
        f"Access token expires in ~{int(token.seconds_until_expiry())}s; "
        f"refresh is automatic from here.\n"
    )


@app.command()
def status() -> None:
    """Show the stored token's state (whoami). Makes no network calls."""
    config = Config.load()
    store = TokenStore(config.token_path)
    # AuthedClient.token_status only reads the store — safe without full creds.
    oauth = YahooOAuthClient(
        client_id=config.yahoo_client_id or "unset",
        client_secret=config.yahoo_client_secret or "unset",
        redirect_uri=config.yahoo_redirect_uri,
        scope=config.yahoo_scope,
    )
    client = AuthedClient(oauth, store, refresh_leeway=DEFAULT_REFRESH_LEEWAY)
    info = client.token_status()
    oauth.close()

    if not info.get("authenticated"):
        console.print(
            f"[yellow]Not authenticated.[/] No token at [cyan]{store.path}[/]. "
            "Run [bold]login[/] first."
        )
        raise typer.Exit(code=1)

    table = Table(title="Yahoo token status", show_header=False, title_style="bold")
    table.add_row("Token file", str(store.path))
    table.add_row("Yahoo GUID", str(info.get("yahoo_guid") or "—"))
    table.add_row("Scope", str(info.get("scope") or config.yahoo_scope))
    table.add_row("Token type", str(info.get("token_type")))
    secs = int(info.get("seconds_until_expiry") or 0)
    if info.get("expired"):
        state_str = f"[red]EXPIRED[/] ({-secs}s ago)"
    elif info.get("near_expiry"):
        state_str = f"[yellow]near expiry[/] (in {secs}s — will auto-refresh)"
    else:
        state_str = f"[green]valid[/] (in {secs}s)"
    table.add_row("Access token", state_str)
    table.add_row("League key", config.yahoo_league_key or "[dim]unset[/]")
    console.print(table)


@app.command()
def logout() -> None:
    """Delete the stored tokens."""
    config = Config.load()
    store = TokenStore(config.token_path)
    if store.clear():
        console.print(f"[green]Removed[/] {store.path}.")
    else:
        console.print(f"[yellow]Nothing to remove[/] at {store.path}.")


@app.command(name="config")
def show_config() -> None:
    """Show which config values are set (secrets masked). No network calls."""
    config = Config.load()

    def mask(value: str) -> str:
        if not value:
            return "[dim]unset[/]"
        if len(value) <= 6:
            return "[green]set[/]"
        return f"[green]set[/] [dim]({value[:3]}…{value[-2:]})[/]"

    table = Table(title="Fantasy Coach config", show_header=True, title_style="bold")
    table.add_column("Variable")
    table.add_column("Value")
    table.add_row("YAHOO_CLIENT_ID", mask(config.yahoo_client_id))
    table.add_row("YAHOO_CLIENT_SECRET", mask(config.yahoo_client_secret))
    table.add_row("YAHOO_REDIRECT_URI", config.yahoo_redirect_uri or "[dim]unset[/]")
    table.add_row("YAHOO_SCOPE", config.yahoo_scope)
    table.add_row("YAHOO_LEAGUE_KEY", config.yahoo_league_key or "[dim]unset[/]")
    table.add_row("Token path", str(config.token_path))
    table.add_row("ODDS_API_KEY", mask(config.odds_api_key))
    table.add_row("FANTASYPROS_API_KEY", mask(config.fantasypros_api_key))
    console.print(table)
    console.print(
        f"\nOAuth ready: "
        + ("[green]yes[/]" if config.has_oauth_credentials else "[red]no[/] — fill .env")
    )


# -- draft companion (M5) ----------------------------------------------------


@app.command()
def draft(
    league: str = typer.Option(
        "", "--league", "-l",
        help="League key ({game}.l.{id}). Defaults to YAHOO_LEAGUE_KEY, then "
        "the store's only league.",
    ),
    team: str = typer.Option(
        "", "--team", "-t",
        help="Your team key. Live mode auto-detects the team you manage; "
        "simulation derives it from --sim-slot.",
    ),
    port: int = typer.Option(8787, "--port", help="Local port for the page."),
    poll: float = typer.Option(
        0.0, "--poll",
        help="Seconds between polls (default: 2.5 live / 1.5 simulated).",
    ),
    simulate: bool = typer.Option(
        False, "--simulate",
        help="No Yahoo, no auth: replay a scripted draft (built from the "
        "stored board) through the identical live loop.",
    ),
    manual: bool = typer.Option(
        False, "--manual",
        help="LIVE DRAFT WITHOUT YAHOO ACCESS: you mark each pick on the page "
        "as it happens (search-as-you-type, one keystroke to confirm, undo). "
        "State persists in the store — restart resumes. --yahoo-sync overlays "
        "Yahoo picks if the API is available.",
    ),
    yahoo_sync: bool = typer.Option(
        False, "--yahoo-sync",
        help="[manual] Best-effort Yahoo draftresults overlay on the manual "
        "stream (needs auth + an approved app; failures are ignored).",
    ),
    reset_draft: bool = typer.Option(
        False, "--reset-draft",
        help="[manual] Forget the picks recorded in the store and start fresh.",
    ),
    sim_slot: int = typer.Option(
        0, "--sim-slot", help="[simulate] Your snake-draft slot (1..teams). "
        "Default: draft.my_slot from the league spec, else 5."
    ),
    sim_speed: int = typer.Option(
        1, "--sim-speed", help="[simulate] Picks revealed per poll."
    ),
    playoff_weight: float = typer.Option(
        -1.0, "--playoff-weight", min=-1.0, max=1.0,
        help="Playoff emphasis w in [0,1]: draft value = (1-w)*season VORP + "
        "w*playoff strength. Default -1 = use PLAYOFF_EMPHASIS from .env "
        "(0.0 = off, pure season value).",
    ),
    injury_weight: float = typer.Option(
        -1.0, "--injury-weight", min=-1.0, max=1.0,
        help="Injury/durability discount weight in [0,1]: 0 = flags only "
        "(ranking unchanged), 1 = full documented discounts. Default -1 = "
        "use INJURY_EMPHASIS from .env (0.0 = off).",
    ),
    risk: float = typer.Option(
        -2.0, "--risk", min=-2.0, max=1.0,
        help="Risk preference in [-1,1]: <0 leans on floors (safe), >0 on "
        "ceilings (upside), 0 = median. Default -2 = use RISK_PREFERENCE "
        "from .env (0.0 = off).",
    ),
    sos_weight: float = typer.Option(
        -1.0, "--sos-weight", min=-1.0, max=1.0,
        help="Per-week strength-of-schedule mix in [0,1] (every week valued "
        "through its own matchup; playoff weeks weighted heavier by the "
        "playoff emphasis on top). Default -1 = use SOS_EMPHASIS from .env.",
    ),
    sim_seed: int = typer.Option(
        0, "--sim-seed", help="[simulate] Seed for the bot room (a different "
        "seed = a different but equally plausible draft)."
    ),
    status_interval: float = typer.Option(
        120.0, "--status-interval", min=15.0,
        help="[live] Seconds between Sleeper injury-status re-checks.",
    ),
    no_warm: bool = typer.Option(
        False, "--no-warm", help="[live] Skip the startup store-warm pass."
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Don't auto-open the page in a browser."
    ),
) -> None:
    """Run the live draft companion next to the Yahoo draft room.

    Live mode polls ``draftresults`` every ~2.5s (inside the client's own
    throttle), rebuilds the drafted set each poll (undo-safe), recomputes the
    available VORP board with baselines that shift as pools drain, weights it
    by your unfilled roster slots, and serves the auto-refreshing board page.
    With a cached schedule and ``--playoff-weight`` > 0 the board blends in
    playoff-week strength and nudges away from bye-week pile-ups.

    ``--simulate`` runs the exact same loop against a scripted snake draft
    generated from the stored board — the full dress rehearsal for draft day.
    """
    import logging
    import threading

    from fantasy_coach.clients.throttle import DRAFT_POLL_INTERVAL
    from fantasy_coach.draft import CompanionServer
    from fantasy_coach.store import CoachStore

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    config = Config.load()
    store = CoachStore(config.db_path)
    weight = config.playoff_emphasis if playoff_weight < 0 else playoff_weight
    inj_weight = config.injury_emphasis if injury_weight < 0 else injury_weight
    risk_pref = config.risk_preference if risk < -1.0 else risk
    sos_w = config.sos_emphasis if sos_weight < 0 else sos_weight

    league_key = league.strip() or config.yahoo_league_key
    if not league_key:
        rows = store.sql("SELECT league_key FROM league_settings")
        if len(rows) == 1:
            league_key = rows[0]["league_key"]
        else:
            console.print(
                "[bold red]No league key.[/] Pass --league, set YAHOO_LEAGUE_KEY, "
                "or warm the store for exactly one league."
            )
            raise typer.Exit(code=1)

    manual_ctl = None
    if manual:
        loop, manual_ctl = _build_manual_loop(
            store, config, league_key, team, sim_slot=sim_slot,
            playoff_weight=weight, injury_weight=inj_weight,
            risk_preference=risk_pref, sos_weight=sos_w,
            reset=reset_draft, yahoo_sync=yahoo_sync,
        )
        poll_interval = poll or 1.5
    elif simulate:
        loop = _build_sim_loop(
            store, config, league_key, team,
            sim_slot=sim_slot, sim_speed=sim_speed, playoff_weight=weight,
            injury_weight=inj_weight, risk_preference=risk_pref,
            sos_weight=sos_w, seed=sim_seed,
        )
        poll_interval = poll or 1.5
    else:
        loop = _build_live_loop(
            store, config, league_key, team, warm=not no_warm,
            playoff_weight=weight, injury_weight=inj_weight,
            status_interval=status_interval, risk_preference=risk_pref,
            sos_weight=sos_w,
        )
        poll_interval = poll or DRAFT_POLL_INTERVAL
    loop.poll_interval = poll_interval

    server = CompanionServer(loop.snapshot, port=port, manual=manual_ctl)
    server.start()
    console.print(
        f"\n[bold green]Draft companion up[/] — open [bold cyan]{server.url}[/] "
        f"next to the Yahoo draft room."
    )
    console.print(
        f"[dim]mode={loop.mode} · league={loop.league_key} · "
        f"team={loop.state.my_team_key or 'unset'} · poll every {poll_interval}s · "
        f"playoff emphasis {weight:g} · injury weight {inj_weight:g} · "
        f"risk {risk_pref:+g} · sos {sos_w:g} · Ctrl+C to stop.[/]\n"
    )
    if not no_browser:
        try:
            import webbrowser

            webbrowser.open(server.url)
        except Exception:  # pragma: no cover - environment dependent
            pass

    try:
        loop.run(threading.Event())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping[/] — draft state is saved in the store.")
    finally:
        server.stop()
        store.close()


def _load_schedule(config: Config, *, refresh: bool):
    """Load the season's schedule (step 5) — cache-first, degrade to ``None``.

    ``refresh=True`` (live warm path) recomputes from nflverse and rewrites
    the cache; failures fall back to the existing cache; no cache at all is a
    warning, never an error — the board simply stays season-only.
    """
    from fantasy_coach.ingest.projections import default_season
    from fantasy_coach.ingest.schedule import ScheduleSource

    season = default_season()
    source = ScheduleSource(cache_dir=config.cache_dir)
    if refresh:
        try:
            return source.warm_cache(season)
        except Exception as exc:
            console.print(
                f"[yellow]schedule refresh failed ({exc}); trying the cache…[/]"
            )
    try:
        return source.load(season)
    except Exception as exc:
        console.print(
            f"[yellow]No schedule available ({exc}) — board stays season-only "
            "(no playoff blend / bye nudge).[/]"
        )
        return None


def _load_durability(config: Config, *, refresh: bool = False):
    """Load durability profiles (step 6) — cache-first, degrade to ``None``.

    Same contract as :func:`_load_schedule`: a refresh failure falls back to
    the cache; no cache at all is a warning, never an error — the board simply
    carries no durability signal.
    """
    from fantasy_coach.ingest.injury import DurabilitySource
    from fantasy_coach.ingest.projections import default_season

    season = default_season()
    source = DurabilitySource(cache_dir=config.cache_dir)
    if refresh:
        try:
            return source.warm_cache(season)
        except Exception as exc:
            console.print(
                f"[yellow]durability refresh failed ({exc}); trying the cache…[/]"
            )
    try:
        return source.load(season)
    except Exception as exc:
        console.print(
            f"[yellow]No durability data available ({exc}) — board carries no "
            "re-injury signal.[/]"
        )
        return None


def _refresh_market(store, config: Config, *, num_teams: int, season: int, refresh: bool) -> list[str]:
    """Free market layer (P0-2/P0-4): Sleeper player catalog + FFC ADP.

    1. **Sleeper catalog** (free, keyless): the full current player universe —
       rookies, team DEFs, current teams, ``yahoo_id``s — merged
       non-destructively into the ``players`` table. Without this, an
       offline-warmed store literally contains no rookies and no defenses.
    2. **FantasyFootballCalculator ADP** (free, keyless): real mock-draft ADP
       for the league's format/size into the ``adp`` table (source ``ffc``) —
       what powers the ADP→VORP gap-fill, the consensus market blend, and
       ADP-anchored survival.

    ``refresh=True`` pulls live and rewrites the caches; ``False`` (the
    offline ``setup-league`` path) serves the caches only. Every failure
    degrades to a warning — prior rows stay.
    """
    from fantasy_coach.ingest.adp import FfcAdpSource, resolve_adp
    from fantasy_coach.ingest.catalog import SleeperCatalogSource

    warnings: list[str] = []
    catalog = SleeperCatalogSource(cache_dir=config.cache_dir)
    try:
        players = catalog.warm_cache() if refresh else catalog.load()
        written = store.upsert_players(players, merge=True)
        folded = store.fold_duplicate_players()
        console.print(
            f"[dim]  Sleeper catalog: {len(players)} players "
            f"(rookies/DEF included) merged — {written} rows touched, "
            f"{folded} gsis-less duplicates folded[/]"
        )
    except Exception as exc:
        if refresh:
            try:
                players = catalog.load()
                store.upsert_players(players, merge=True)
                warnings.append(f"Sleeper catalog pull failed ({exc}); cache used")
            except Exception:
                warnings.append(f"Sleeper catalog unavailable ({exc}); prior players kept")
        else:
            warnings.append(f"no Sleeper catalog cache ({exc}); prior players kept")

    scoring_format = "ppr"  # full PPR confirmed for Sam's league
    ffc = FfcAdpSource(scoring_format=scoring_format, teams=num_teams, cache_dir=config.cache_dir)
    try:
        records = ffc.warm_cache(season) if refresh else ffc.load(season)
        rows, unresolved = resolve_adp(
            records, store.sql("SELECT canonical_id, name, position, team FROM players")
        )
        written = store.upsert_adp(rows, source="ffc")
        console.print(
            f"[dim]  FFC ADP ({scoring_format}, {num_teams} teams): "
            f"{written}/{len(records)} resolved onto players[/]"
        )
        if unresolved:
            console.print(
                f"[dim]  {len(unresolved)} ADP names unresolved (first: {unresolved[0]})[/]"
            )
    except Exception as exc:
        if refresh:
            warnings.append(f"FFC ADP pull failed ({exc}); prior adp rows kept")
        else:
            warnings.append(f"no FFC ADP cache ({exc}); prior adp rows kept")
    return warnings


def _crosswalk_players(yahoo_players):
    """Run the M3 crosswalk over a pulled Yahoo player universe.

    Resolves every Yahoo identity through the DynastyProcess id map (a live
    ``nfl_data_py.import_ids()`` pull) and attaches Yahoo ADP, returning the
    ``players=`` input :func:`warm_store` needs. This is what stamps
    ``yahoo_id`` onto the store's player rows — without it, live draft picks
    (which arrive as Yahoo ids) cannot resolve to board identities. Raises on
    failure; callers degrade to a warning and the store's prior rows.
    """
    from fantasy_coach.ingest import IdResolver, build_player_index, load_id_crosswalk

    crosswalk = load_id_crosswalk()
    index = build_player_index(
        (p.identity() for p in yahoo_players), IdResolver(crosswalk)
    )
    index.attach_yahoo_market(
        {
            p.player_id: {"adp": p.average_draft_pick}
            for p in yahoo_players
            if p.player_id
        }
    )
    stats = index.report.summary()
    console.print(
        f"[dim]  crosswalk: {stats['matched']}/{stats['total']} matched "
        f"({stats['unmatched']} unmatched, {stats['needs_review']} to review)[/]"
    )
    return list(index.players.values())


def _vintage_table(store, title: str) -> None:
    """Print the store's data_vintage rows as a rich table."""
    rows = store.vintage()
    table = Table(title=title, title_style="bold", show_header=True)
    table.add_column("Data scope")
    table.add_column("Last refreshed (UTC)")
    table.add_column("Detail", overflow="fold")
    for row in rows:
        table.add_row(row["scope"], row["refreshed_at"], row["detail"] or "")
    if not rows:
        table.add_row("[dim]— empty store —[/]", "", "")
    console.print(table)


@app.command()
def vintage() -> None:
    """Show how fresh every stored data slice is. No network calls."""
    from fantasy_coach.store import CoachStore

    config = Config.load()
    with CoachStore(config.db_path) as store:
        _vintage_table(store, f"Data vintage — {config.db_path}")
    console.print(
        "[dim]Live sources: Yahoo status (during the draft), Sleeper status "
        "(re-pulled on refresh + every ~2 min in the live loop). Periodic: "
        "nflverse projections/schedule/durability (refresh re-pulls them).[/]"
    )


@app.command()
def refresh(
    league: str = typer.Option(
        "", "--league", "-l",
        help="League key. Defaults to YAHOO_LEAGUE_KEY, then the store's only league.",
    ),
    skip_yahoo: bool = typer.Option(
        False, "--skip-yahoo",
        help="Skip the Yahoo pulls (no auth needed; Sleeper + nflverse still refresh).",
    ),
    max_yahoo_players: int = typer.Option(
        300, "--max-yahoo-players",
        help="How deep to re-pull Yahoo statuses/ADP (sorted by rank; 25/request).",
    ),
) -> None:
    """Re-pull every risk-relevant source to its most up-to-date state.

    The pre-draft freshness pass: re-warms nflverse projections, schedule and
    durability caches, pulls current injury statuses from Sleeper (free, no
    key) and — when authed — Yahoo statuses + ADP, rebuilds the value board,
    and prints the before/after data vintage so stale slices are obvious.
    Every step degrades to a warning; whatever cannot refresh keeps its prior
    rows and its prior (visible) vintage.
    """
    from fantasy_coach.ingest.consensus import market_adp_from_players
    from fantasy_coach.ingest.injury import (
        InjuryReport,
        SleeperStatusSource,
        normalize_status,
    )
    from fantasy_coach.ingest.projections import default_season, make_projection_source
    from fantasy_coach.ingest.schedule import ScheduleSource
    from fantasy_coach.store import CoachStore, warm_store

    config = Config.load()
    store = CoachStore(config.db_path)
    season = default_season()

    league_key = league.strip() or config.yahoo_league_key
    if not league_key:
        rows = store.sql("SELECT league_key FROM league_settings")
        league_key = rows[0]["league_key"] if len(rows) == 1 else ""

    _vintage_table(store, "Before refresh")
    warnings: list[str] = []

    # 0. The free market layer FIRST (P0-2/P0-4): Sleeper catalog (rookies +
    #    DEFs + yahoo ids) and FFC ADP — the consensus projections built next
    #    read the store's ADP lazily, so this order is what makes the default
    #    model+market blend actually see the market.
    settings_row = store.league_settings(league_key) if league_key else None
    market_teams = (settings_row.max_teams if settings_row else None) or 10
    console.print("[dim]Refreshing free market data (Sleeper catalog + FFC ADP)…[/]")
    warnings.extend(
        _refresh_market(store, config, num_teams=market_teams, season=season, refresh=True)
    )

    # 1. Projections (periodic source — recomputed from latest data). The
    #    configured source is built here: consensus reads the store's current
    #    ADP lazily (whatever this refresh / a prior warm persisted).
    projection_source = make_projection_source(
        config, market=lambda: market_adp_from_players(store.canonical_players())
    )
    console.print(f"[dim]Refreshing {projection_source.name} projections for {season}…[/]")
    try:
        warm = getattr(projection_source, "warm_cache", None)
        if callable(warm):
            warm(season)
    except Exception as exc:
        warnings.append(f"projections refresh failed ({exc}); cache/store rows kept")

    # 2. nflverse schedule + SOS.
    console.print("[dim]Refreshing schedule + opponent difficulty…[/]")
    schedule = None
    try:
        schedule = ScheduleSource(cache_dir=config.cache_dir).warm_cache(season)
        store.stamp_vintage(f"schedule:nflverse:{season}")
    except Exception as exc:
        warnings.append(f"schedule refresh failed ({exc}); trying prior cache")
        schedule = _load_schedule(config, refresh=False)

    # 3. nflverse durability (games missed + injury-report history).
    console.print("[dim]Refreshing durability profiles…[/]")
    durability = _load_durability(config, refresh=True)

    # 4. Sleeper current injury status (live-ish: free, keyless, frequent).
    console.print("[dim]Pulling current injury statuses from Sleeper…[/]")
    try:
        reports = SleeperStatusSource.for_players(store.canonical_players()).fetch()
        written = store.upsert_injury_reports(reports, source="sleeper")
        flagged = sum(1 for r in reports.values() if r.status)
        console.print(
            f"[dim]  {written} players checked · {flagged} carrying a designation[/]"
        )
    except Exception as exc:
        warnings.append(f"Sleeper status pull failed ({exc}); prior reports kept")

    # 5. Yahoo statuses + ADP (live during drafts; needs auth).
    settings = store.league_settings(league_key) if league_key else None
    cplayers = None
    if not skip_yahoo and config.has_oauth_credentials and league_key:
        try:
            from fantasy_coach.auth.session import get_authed_client
            from fantasy_coach.clients.yahoo import YahooClient

            yahoo = YahooClient(get_authed_client(config))
            console.print(f"[dim]Refreshing Yahoo settings/status/ADP for {league_key}…[/]")
            settings = yahoo.get_league_settings(league_key)
            players = yahoo.get_players(
                league_key,
                sort="OR",
                out=("draft_analysis",),
                max_players=max_yahoo_players,
            )
            # Crosswalk the pulled universe so player rows carry yahoo_ids
            # (what live picks resolve through); fall back to prior rows.
            try:
                cplayers = _crosswalk_players(players)
                by_yahoo = {
                    p.ids.yahoo_id: p.canonical_id
                    for p in cplayers
                    if p.ids.yahoo_id
                }
            except Exception as exc:
                warnings.append(
                    f"id crosswalk failed ({exc}); resolving via stored player rows"
                )
                by_yahoo = {
                    row["yahoo_id"]: row["canonical_id"]
                    for row in store.sql(
                        "SELECT yahoo_id, canonical_id FROM players WHERE yahoo_id IS NOT NULL"
                    )
                }
            yahoo_reports: dict[str, InjuryReport] = {}
            adp_rows = []
            for p in players:
                cid = by_yahoo.get(p.player_id)
                if cid is None:
                    continue
                yahoo_reports[cid] = InjuryReport(
                    source="yahoo",
                    status=normalize_status(p.status),
                    raw_status=p.status,
                    detail=p.injury_note,
                )
                if p.average_draft_pick is not None:
                    adp_rows.append(
                        {"canonical_id": cid, "average_pick": p.average_draft_pick}
                    )
            store.upsert_injury_reports(yahoo_reports, source="yahoo")
            if adp_rows:
                store.upsert_adp(adp_rows, source="yahoo")
            console.print(
                f"[dim]  {len(yahoo_reports)} statuses · {len(adp_rows)} ADP rows updated[/]"
            )
        except Exception as exc:
            warnings.append(f"Yahoo refresh failed ({exc}); prior statuses/ADP kept")
    elif not skip_yahoo:
        warnings.append(
            "Yahoo skipped (no OAuth credentials or league key) — its statuses "
            "refresh live during the draft anyway"
        )

    # 6. Rebuild the board from the refreshed store.
    if settings is not None:
        result = warm_store(
            store,
            settings,
            projection_source=projection_source,
            players=cplayers,
            schedule=schedule,
            playoff_weight=config.playoff_emphasis,
            durability=durability,
            injury_weight=config.injury_emphasis,
            risk_preference=config.risk_preference,
            sos_weight=config.sos_emphasis,
        )
        console.print("[dim]" + result.summary() + "[/]")
        warnings.extend(result.warnings)
    else:
        warnings.append(
            "no league settings (stored or from Yahoo) — caches refreshed but "
            "the board was not rebuilt"
        )

    _vintage_table(store, "After refresh")
    for warning in warnings:
        console.print(f"[yellow]WARNING:[/] {warning}")
    console.print(
        "\n[dim]Source freshness, honestly: Yahoo status is live while the "
        "draft loop polls; Sleeper status is frequently updated and was just "
        "re-pulled; nflverse projections/schedule/durability are periodic "
        "datasets — 'refreshed' means recomputed from their latest publish, "
        "not real-time.[/]"
    )
    store.close()


def _load_spec(config: Config, league_key: str = ""):
    """The offline league spec when present and matching ``league_key``.

    Returns ``None`` (never raises) when the file is missing or describes a
    different league — the store's stored settings then stand alone.
    """
    from fantasy_coach.league import load_league_spec

    path = config.league_file
    if not path.exists():
        return None
    try:
        spec = load_league_spec(path)
    except Exception as exc:
        console.print(f"[yellow]league spec {path} unreadable ({exc}) — ignored[/]")
        return None
    if league_key and spec.league_key != league_key:
        return None
    return spec


@app.command(name="setup-league")
def setup_league(
    file: str = typer.Option(
        "", "--file", "-f",
        help="League spec JSON. Default: FANTASY_COACH_LEAGUE_FILE / data/league.json.",
    ),
    no_warm: bool = typer.Option(
        False, "--no-warm", help="Store the settings only; don't rebuild the board."
    ),
) -> None:
    """Load your league's exact rules offline and build the board for them.

    Reads the spec (scoring per stat, the full lineup — IDP slots, no
    kickers, flex-only TE, whatever the league does — playoff weeks, draft
    length, keeper rules + keepers), stores the settings under the spec's
    league key, and rebuilds the value board from the local projection /
    schedule / durability caches with replacement baselines derived from
    THIS roster across THIS many teams. Runs with zero Yahoo access; run
    ``refresh --skip-yahoo`` afterwards whenever you want fresher data.
    """
    from fantasy_coach.ingest.consensus import market_adp_from_players
    from fantasy_coach.ingest.projections import default_season, make_projection_source
    from fantasy_coach.league import load_league_spec, resolve_keepers
    from fantasy_coach.store import CoachStore, warm_store
    from fantasy_coach.value.board import startable_positions

    config = Config.load()
    path = file.strip() or str(config.league_file)
    try:
        spec = load_league_spec(path)
    except Exception as exc:
        console.print(f"[bold red]Could not read league spec {path}:[/] {exc}")
        raise typer.Exit(code=1)
    settings = spec.settings
    console.print(
        f"[bold]{spec.name or spec.league_key}[/] — {spec.num_teams} teams, "
        f"{spec.rounds} rounds, playoffs wk{settings.playoff_start_week}+ "
        f"({settings.num_playoff_teams} teams), startable: "
        + ", ".join(sorted(startable_positions(settings)))
        + (" · keeper league" if spec.is_keeper_league else "")
    )
    store = CoachStore(config.db_path)
    store.upsert_league_settings(settings, num_teams=spec.num_teams)
    store.stamp_vintage(f"league_spec:{spec.league_key}", detail=str(path))
    if no_warm:
        console.print(f"[green]Stored settings for {spec.league_key}.[/]")
        store.close()
        return
    season = default_season()
    # Free market layer from the local caches (offline command — run
    # ``refresh --skip-yahoo`` when online to re-pull them).
    for w in _refresh_market(
        store, config, num_teams=spec.num_teams, season=season, refresh=False
    ):
        console.print(f"[yellow]market:[/] {w}")
    projection_source = make_projection_source(
        config, market=lambda: market_adp_from_players(store.canonical_players())
    )
    result = warm_store(
        store,
        settings,
        projection_source=projection_source,
        num_teams=spec.num_teams,
        season=season,
        schedule=_load_schedule(config, refresh=False),
        playoff_weight=config.playoff_emphasis,
        durability=_load_durability(config),
        injury_weight=config.injury_emphasis,
        risk_preference=config.risk_preference,
        sos_weight=config.sos_emphasis,
    )
    console.print("[dim]" + result.summary() + "[/]")
    meta = store.board_meta(spec.league_key)
    if meta is not None:
        import json as _json

        baselines = _json.loads(meta["baselines"])
        console.print(
            "[dim]replacement baselines (league pts): "
            + ", ".join(f"{p} {v:.0f}" for p, v in sorted(baselines.items()))
            + "[/]"
        )
    if spec.keepers:
        resolved, warns = resolve_keepers(spec, store.sql("SELECT canonical_id, name, position FROM players"))
        for k in resolved:
            store.upsert_keeper(
                spec.league_key, team_key=k.team_key, canonical_id=k.canonical_id,
                cost_round=k.round, name=k.name, position=k.position,
                last_round=k.last_round, source="spec",
            )
        console.print(
            f"[dim]keepers: {len(resolved)} resolved and stored (edit live on the "
            f"draft page or in the spec + re-run)[/]"
        )
        for w in warns:
            console.print(f"[yellow]keeper warning:[/] {w}")
    stored = store.keepers(spec.league_key)
    if stored:
        console.print(f"[dim]{len(stored)} keeper row(s) in the store for {spec.league_key}[/]")
    for note in spec.notes:
        console.print(f"[dim]note: {note}[/]")
    store.close()


def _snake_pick(slot: int, round_no: int, num_teams: int) -> int:
    """The overall pick number slot ``slot`` owns in ``round_no`` (snake)."""
    if round_no % 2 == 1:
        return (round_no - 1) * num_teams + slot
    return (round_no - 1) * num_teams + (num_teams - slot + 1)


@app.command(name="keeper-value")
def keeper_value(
    candidate: str = typer.Option(
        "", "--candidate", "-c",
        help="Evaluate one candidate keeper by name (e.g. \"Puka Nacua\"). "
        "Without it, every keeper stored for the league is evaluated.",
    ),
    last_round: int = typer.Option(
        0, "--last-round",
        help="[candidate] The round he was drafted in LAST year (cost = that "
        "− cost_rounds_earlier). 0 = undrafted (costs the undrafted round).",
    ),
    cost_round: int = typer.Option(
        0, "--cost-round",
        help="[candidate] Override the cost round directly (skips the rule).",
    ),
    slot: int = typer.Option(
        0, "--slot", help="Your round-1 draft slot. Default: draft.my_slot "
        "from data/league.json, else mid-draft.",
    ),
    league: str = typer.Option("", "--league", "-l", help="League key."),
) -> None:
    """Keeper surplus (P1-5): is keeping a player worth the pick he costs?

    ``surplus = value(player) − value of the expected best available at your
    pick in his cost round`` — computed from the STORED board with every
    stored keeper already removed from the pool (they're gone for everyone).
    A positive surplus means the keeper beats what that pick would have
    bought you; rank the candidates by surplus and keep the top ones (up to
    ``keeper_rules.max_keepers``). Run before Sep 1 with a fresh board
    (``refresh --skip-yahoo``).
    """
    from fantasy_coach.ingest.names import clean_name as _clean
    from fantasy_coach.store import CoachStore

    config = Config.load()
    store = CoachStore(config.db_path)
    league_key = league.strip() or config.yahoo_league_key
    if not league_key:
        rows = store.sql("SELECT league_key FROM league_settings")
        if len(rows) == 1:
            league_key = rows[0]["league_key"]
        else:
            console.print("[bold red]No league key[/] — pass --league or set YAHOO_LEAGUE_KEY.")
            raise typer.Exit(code=1)
    board = store.get_board(league_key)
    if not board:
        console.print(f"[bold red]No stored board for {league_key}[/] — run setup-league / refresh first.")
        raise typer.Exit(code=1)
    settings = store.league_settings(league_key)
    num_teams = (settings.max_teams if settings else None) or 10
    spec = _load_spec(config, league_key)
    rules = spec.keeper_rules if spec else None
    rounds = spec.rounds if spec else 17
    if slot == 0 and spec is not None and spec.my_slot is not None:
        slot = spec.my_slot
    if slot == 0:
        slot = (num_teams + 1) // 2
        console.print(f"[yellow]No draft slot known — assuming mid-draft slot {slot} "
                      "(set draft.my_slot or pass --slot).[/]")

    def value_of(row) -> float:
        return row["draft_value"] if row["draft_value"] is not None else row["vorp"]

    keeper_rows = store.keepers(league_key)
    kept_ids = {str(r["canonical_id"]) for r in keeper_rows}
    pool = sorted(
        (r for r in board if str(r["canonical_id"]) not in kept_ids),
        key=value_of, reverse=True,
    )

    def expected_at(pick_no: int):
        """The expected best available at overall pick ``pick_no`` — the
        pool's ``pick_no``-th best (picks 1..n−1 remove the top n−1)."""
        idx = min(len(pool) - 1, max(0, pick_no - 1))
        return pool[idx]

    def surplus_line(name: str, position: str, value: float, r_cost: int, team_slot: int):
        pick_no = _snake_pick(team_slot, r_cost, num_teams)
        alt = expected_at(pick_no)
        return {
            "name": name, "position": position, "value": value,
            "cost_round": r_cost, "pick": pick_no,
            "alt_name": alt["name"], "alt_pos": alt["position"],
            "alt_value": value_of(alt), "surplus": value - value_of(alt),
        }

    if candidate.strip():
        from fantasy_coach.league import KeeperRules, keeper_cost_round

        cand_rules = rules or KeeperRules()
        target = _clean(candidate)
        matches = [r for r in board if _clean(r["name"]) == target]
        if not matches:
            matches = [r for r in board if target in _clean(r["name"])]
        if not matches:
            console.print(f"[bold red]{candidate!r} not on the stored board.[/]")
            raise typer.Exit(code=1)
        row = matches[0]
        if cost_round > 0:
            r_cost = cost_round
        else:
            try:
                r_cost = keeper_cost_round(last_round or None, cand_rules)
            except Exception as exc:
                console.print(f"[bold red]Keeper rules:[/] {exc}")
                raise typer.Exit(code=1)
        line = surplus_line(row["name"], row["position"], value_of(row), r_cost, slot)
        verdict = "KEEP" if line["surplus"] > 0 else "PASS"
        console.print(
            f"\n[bold]{line['name']}[/] ({line['position']}) — board value "
            f"{line['value']:+.1f}, kept at your round-{r_cost} pick "
            f"(overall #{line['pick']}, slot {slot})."
        )
        console.print(
            f"Expected best available there instead: "
            f"[bold]{line['alt_name']}[/] ({line['alt_pos']}, {line['alt_value']:+.1f})."
        )
        console.print(
            f"[bold {'green' if line['surplus'] > 0 else 'red'}]Surplus "
            f"{line['surplus']:+.1f} -> {verdict}[/] "
            f"[dim](positive = the keeper beats the pick he costs; compare "
            f"your candidates and keep the top {cand_rules.max_keepers})[/]"
        )
        store.close()
        return

    # League-wide view: every stored keeper's surplus + what the pool loses.
    if not keeper_rows:
        console.print(
            "[yellow]No keepers stored yet.[/] Evaluate candidates with "
            "--candidate \"Name\" --last-round N, or enter keepers via "
            "data/league.json / the draft page."
        )
        store.close()
        return
    by_board = {str(r["canonical_id"]): r for r in board}
    table = Table(title=f"Stored keepers — surplus vs the pick they cost ({league_key})",
                  title_style="bold", show_header=True)
    for col in ("Team", "Keeper", "Pos", "Cost Rd", "Pick", "Value", "Best available there", "Surplus"):
        table.add_column(col)
    total_removed: dict[str, float] = {}
    for k in keeper_rows:
        row = by_board.get(str(k["canonical_id"]))
        team_key = str(k["team_key"])
        try:
            team_slot = int(team_key.rsplit(".", 1)[-1])
        except ValueError:
            team_slot = slot
        if row is None:
            table.add_row(team_key, str(k["name"]), str(k["position"]),
                          str(k["cost_round"]), "—", "off-board", "—", "—")
            continue
        line = surplus_line(row["name"], row["position"], value_of(row),
                            int(k["cost_round"]), team_slot)
        total_removed[team_key] = total_removed.get(team_key, 0.0) + max(0.0, line["value"])
        table.add_row(
            team_key.rsplit(".", 1)[-1], line["name"], line["position"],
            str(line["cost_round"]), str(line["pick"]), f"{line['value']:+.1f}",
            f"{line['alt_name']} ({line['alt_value']:+.1f})",
            f"[{'green' if line['surplus'] > 0 else 'red'}]{line['surplus']:+.1f}[/]",
        )
    console.print(table)
    console.print(
        "[dim]Pool impact (positive board value each team removes via keepers): "
        + ", ".join(f"slot {t.rsplit('.', 1)[-1]}: {v:.0f}" for t, v in sorted(total_removed.items()))
        + "[/]"
    )
    store.close()


def _league_keepers(store, config: Config, league_key: str, spec):
    """``(team_key, cost_round, canonical_id)`` for the draft — the store's
    keeper table (what the page edits), seeded from the spec when empty."""
    rows = store.keepers(league_key)
    if rows:
        return [(str(r["team_key"]), int(r["cost_round"]), str(r["canonical_id"])) for r in rows]
    if spec is not None and spec.keepers:
        from fantasy_coach.league import resolve_keepers

        resolved, warns = resolve_keepers(
            spec, store.sql("SELECT canonical_id, name, position FROM players")
        )
        for w in warns:
            console.print(f"[yellow]keeper warning:[/] {w}")
        for k in resolved:
            store.upsert_keeper(
                league_key, team_key=k.team_key, canonical_id=k.canonical_id,
                cost_round=k.round, name=k.name, position=k.position,
                last_round=k.last_round, source="spec",
            )
        return [(k.team_key, k.round, k.canonical_id) for k in resolved]
    return []


def _build_manual_loop(
    store, config: Config, league_key: str, team: str,
    *, sim_slot: int, playoff_weight: float, injury_weight: float,
    risk_preference: float, sos_weight: float, reset: bool, yahoo_sync: bool,
):
    """Wire the manual-entry live loop: stored settings + a hand-fed pick list.

    Returns ``(loop, ManualDraft)``. The pick list is prebuilt from the league
    spec (teams, rounds, keepers, your slot); previously recorded picks are
    restored from the store unless ``reset``.
    """
    from fantasy_coach.draft import DraftLoop
    from fantasy_coach.draft.manual import ManualDraft, ManualPickSource, PlayerFinder
    from fantasy_coach.draft.simulate import sim_team_names

    settings = store.league_settings(league_key)
    if settings is None:
        console.print(
            f"[bold red]No stored settings for {league_key}[/] — run "
            "[bold]setup-league[/] first (data/league.json)."
        )
        raise typer.Exit(code=1)
    num_teams = settings.max_teams or 10
    spec = _load_spec(config, league_key)
    rounds = spec.rounds if spec else max(1, settings.roster_size - settings.injury_slots)
    keeper_rules = spec.keeper_rules if spec else None
    if spec is not None and spec.my_slot is not None and sim_slot == 0:
        sim_slot = spec.my_slot
    if not team.strip() and sim_slot == 0:
        console.print(
            "[bold red]Which slot are you?[/] Pass --sim-slot N (your round-1 draft "
            "position) or set draft.my_slot in data/league.json."
        )
        raise typer.Exit(code=1)
    team_key = team.strip() or f"{league_key}.t.{sim_slot}"
    order = [f"{league_key}.t.{slot}" for slot in range(1, num_teams + 1)]
    team_names = sim_team_names(league_key, num_teams, sim_slot or 0)
    if spec is not None:
        team_names.update(spec.team_names)  # "teams" in the spec, else "Team N"
    keepers = _league_keepers(store, config, league_key, spec)
    if keepers:
        console.print(f"[dim]{len(keepers)} keeper(s) pre-marked (their cost-round picks are taken).[/]")

    source = ManualPickSource(order, rounds, game_code=league_key.split(".", 1)[0], keepers=keepers)
    if reset:
        removed = store.clear_draft_picks(league_key)
        console.print(f"[yellow]Reset:[/] forgot {removed} recorded pick(s).")
    else:
        restored = source.restore(store.draft_picks(league_key))
        if restored:
            console.print(f"[green]Resumed:[/] {restored} pick(s) restored from the store.")
    if yahoo_sync:
        try:
            from fantasy_coach.auth.session import get_authed_client
            from fantasy_coach.clients.yahoo import YahooClient
            from fantasy_coach.draft import YahooPickSource

            config.require_oauth()
            source.overlay(YahooPickSource(YahooClient(get_authed_client(config)), league_key))
            console.print("[dim]Yahoo auto-sync overlay enabled (best effort).[/]")
        except Exception as exc:
            console.print(f"[yellow]Yahoo sync unavailable ({exc}) — manual entry only.[/]")

    loop = DraftLoop(
        store,
        settings,
        source,
        my_team_key=team_key,
        league_key=league_key,
        mode="manual",
        team_names=team_names,
        record_to_store=True,  # persistence: restart resumes from draft_picks
        schedule=_load_schedule(config, refresh=False),
        playoff_weight=playoff_weight,
        injury_weight=injury_weight,
        risk_preference=risk_preference,
        sos_weight=sos_weight,
        draft_order=order,
        keeper_rules=keeper_rules,
    )
    finder = PlayerFinder(
        {
            "canonical_id": r["canonical_id"], "name": r["name"], "position": r["position"],
            "team": r["team"], "overall_rank": r["overall_rank"],
            "raw_id": r["canonical_id"],
        }
        for r in store.sql(
            "SELECT p.canonical_id, p.name, p.position, p.team, b.overall_rank "
            "FROM players p LEFT JOIN value_board b "
            "ON b.canonical_id = p.canonical_id AND b.league_key = ?",
            [league_key],
        )
    )
    console.print(
        f"[dim]Manual entry: {num_teams} teams × {rounds} rounds ({num_teams * rounds} picks); "
        f"you are {team_names.get(team_key, team_key)} — mark picks on the page as they happen.[/]"
    )
    from fantasy_coach.draft.manual import KeeperBook

    book = KeeperBook(store, league_key, keeper_rules, rounds=rounds) if keeper_rules else None
    ctl = ManualDraft(source=source, loop=loop, finder=finder, team_names=team_names, keepers=book)
    if book is not None:
        loop.set_keeper_labels({str(r["canonical_id"]): f"keeper · Rd {r['cost_round']}" for r in book.rows()})
    return loop, ctl


def _build_sim_loop(
    store, config: Config, league_key: str, team: str,
    *, sim_slot: int, sim_speed: int, playoff_weight: float,
    injury_weight: float = 0.0, risk_preference: float = 0.0,
    sos_weight: float = 0.0, seed: int = 0,
):
    """Wire the offline simulation loop: stored settings + scripted picks."""
    from fantasy_coach.draft import DraftLoop, SimulatedPickSource, script_draft, sim_team_names

    settings = store.league_settings(league_key)
    if settings is None:
        console.print(
            f"[bold red]No stored settings for {league_key}[/] in "
            f"[cyan]{config.db_path}[/]. Warm the store first (simulation runs "
            "entirely from the store) — and note the path is relative to the "
            "current directory: run from the project root or set "
            "FANTASY_COACH_DB_PATH."
        )
        raise typer.Exit(code=1)
    num_teams = settings.max_teams or 12
    spec = _load_spec(config, league_key)
    keepers = None
    keeper_rules = None
    rounds = None
    if spec is not None:
        rounds = spec.rounds
        keeper_rules = spec.keeper_rules
        if spec.my_slot is not None and sim_slot == 0:
            sim_slot = spec.my_slot
    keepers = _league_keepers(store, config, league_key, spec) or None
    if keepers:
        console.print(f"[dim]{len(keepers)} keeper(s) scripted into the draft.[/]")
    sim_slot = sim_slot or 5
    team_key = team.strip() or f"{league_key}.t.{sim_slot}"
    script = script_draft(
        store, settings, league_key=league_key, seed=seed, rounds=rounds, keepers=keepers
    )
    console.print(
        f"[dim]Simulating a {num_teams}-team, {len(script) // num_teams}-round "
        f"snake draft ({len(script)} picks, bot seed {seed}); you draft from "
        f"slot {sim_slot}.[/]"
    )
    return DraftLoop(
        store,
        settings,
        SimulatedPickSource(script, picks_per_poll=sim_speed),
        my_team_key=team_key,
        league_key=league_key,
        mode="simulation",
        team_names=sim_team_names(league_key, num_teams, sim_slot),
        record_to_store=False,  # never write sim picks over a real league's table
        schedule=_load_schedule(config, refresh=False),
        playoff_weight=playoff_weight,
        injury_weight=injury_weight,  # flags/discounts from stored reports only
        risk_preference=risk_preference,
        sos_weight=sos_weight,
        draft_order=[f"{league_key}.t.{slot}" for slot in range(1, num_teams + 1)],
        keeper_rules=keeper_rules,
    )


def _build_live_loop(
    store, config: Config, league_key: str, team: str,
    *, warm: bool, playoff_weight: float, injury_weight: float = 0.0,
    status_interval: float = 120.0, risk_preference: float = 0.0,
    sos_weight: float = 0.0,
):
    """Wire the live loop: authed Yahoo client, settings, keepers, warm pass."""
    from fantasy_coach.auth.session import get_authed_client
    from fantasy_coach.clients.yahoo import YahooClient
    from fantasy_coach.draft import DraftLoop, YahooPickSource
    from fantasy_coach.ingest.consensus import market_adp_from_players
    from fantasy_coach.ingest.injury import SleeperStatusSource
    from fantasy_coach.ingest.projections import make_projection_source
    from fantasy_coach.store import warm_store

    try:
        config.require_oauth()
    except ConfigError as exc:
        console.print(f"[bold red]Configuration error:[/] {exc}")
        raise typer.Exit(code=1)
    yahoo = YahooClient(get_authed_client(config))  # default throttle = 2.5s

    console.print(f"[dim]Fetching settings for {league_key}…[/]")
    settings = yahoo.get_league_settings(league_key)

    schedule = _load_schedule(config, refresh=warm)
    if warm:
        # Crosswalk the league's player universe so store rows carry yahoo_ids
        # — the id space live draft picks arrive in. Degrades to prior rows.
        players = None
        try:
            console.print(f"[dim]Crosswalking {league_key} players (Yahoo → canonical ids)…[/]")
            players = _crosswalk_players(
                yahoo.get_players(
                    league_key, sort="OR", out=("draft_analysis",), max_players=400
                )
            )
        except Exception as exc:
            console.print(
                f"[yellow]player crosswalk failed ({exc}) — keeping prior player "
                "rows; live picks resolve only if a past warm stamped yahoo ids[/]"
            )
        # Consensus (when selected) blends against the freshest ADP on hand:
        # the just-crosswalked players, else whatever the store already holds.
        result = warm_store(
            store, settings,
            projection_source=make_projection_source(
                config,
                market=lambda: market_adp_from_players(
                    players if players is not None else store.canonical_players()
                ),
            ),
            players=players,
            schedule=schedule,
            playoff_weight=playoff_weight,
            durability=_load_durability(config),
            injury_weight=injury_weight,
            risk_preference=risk_preference,
            sos_weight=sos_weight,
        )
        console.print("[dim]" + result.summary() + "[/]")

    status_source = SleeperStatusSource.for_players(store.canonical_players())

    teams = yahoo.get_league_teams(league_key)
    team_names = {t.team_key: t.name for t in teams}
    team_key = team.strip()
    if not team_key:
        mine = [t for t in teams if t.is_owned_by_current_login]
        if len(mine) != 1:
            console.print(
                "[bold red]Could not auto-detect your team[/] — pass "
                "--team {league}.t.{id}. Teams: "
                + ", ".join(f"{t.team_key} ({t.name})" for t in teams)
            )
            raise typer.Exit(code=1)
        team_key = mine[0].team_key
        console.print(f"[dim]Your team: {team_names[team_key]} ({team_key})[/]")

    loop = DraftLoop(
        store,
        settings,
        YahooPickSource(yahoo, league_key),
        my_team_key=team_key,
        league_key=league_key,
        mode="live",
        team_names=team_names,
        schedule=schedule,
        playoff_weight=playoff_weight,
        status_source=status_source,
        status_interval=status_interval,
        injury_weight=injury_weight,
        risk_preference=risk_preference,
        sos_weight=sos_weight,
        keeper_rules=(spec.keeper_rules if (spec := _load_spec(config, league_key)) else None),
    )

    # Seed keepers / pre-rostered players so they are never recommended.
    console.print(f"[dim]Seeding pre-draft rosters ({len(teams)} teams)…[/]")
    for t in teams:
        try:
            roster = yahoo.get_team_roster(t.team_key)
        except Exception as exc:
            console.print(f"[yellow]roster read failed for {t.team_key}: {exc}[/]")
            continue
        seeded = loop.seed_keepers(
            t.team_key, [p.player_id for p in roster.players if p.player_id]
        )
        if seeded:
            console.print(f"[dim]  {team_names[t.team_key]}: {seeded} keeper(s)[/]")
    return loop


def _extract_state(user_input: str) -> str | None:
    """Return the ``state`` param if the user pasted a full redirect URL."""
    text = user_input.strip()
    if "state=" not in text:
        return None
    query = urlparse(text).query or text.lstrip("?")
    values = parse_qs(query).get("state")
    return values[0] if values else None


def main() -> None:
    """Entry point for ``python -m fantasy_coach`` and the console script."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
