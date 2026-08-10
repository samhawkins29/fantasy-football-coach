"""Command-line interface for Fantasy Coach.

Commands:
    login    Run the Yahoo OAuth consent flow and store tokens.
    status   Show the stored token's state (a.k.a. whoami). No network.
    logout   Delete the stored tokens.
    config   Show which config values are set (secrets masked). No network.
    draft    The live draft companion (M5) — poll the draft room, serve the
             recommendation board at http://localhost:<port>. ``--simulate``
             replays a scripted draft offline through the identical loop.

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
    sim_slot: int = typer.Option(
        5, "--sim-slot", help="[simulate] Your snake-draft slot (1..teams)."
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

    if simulate:
        loop = _build_sim_loop(
            store, config, league_key, team,
            sim_slot=sim_slot, sim_speed=sim_speed, playoff_weight=weight,
        )
        poll_interval = poll or 1.5
    else:
        loop = _build_live_loop(
            store, config, league_key, team, warm=not no_warm, playoff_weight=weight
        )
        poll_interval = poll or DRAFT_POLL_INTERVAL
    loop.poll_interval = poll_interval

    server = CompanionServer(loop.snapshot, port=port)
    server.start()
    console.print(
        f"\n[bold green]Draft companion up[/] — open [bold cyan]{server.url}[/] "
        f"next to the Yahoo draft room."
    )
    console.print(
        f"[dim]mode={loop.mode} · league={loop.league_key} · "
        f"team={loop.state.my_team_key or 'unset'} · poll every {poll_interval}s · "
        f"playoff emphasis {weight:g} · Ctrl+C to stop.[/]\n"
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


def _build_sim_loop(
    store, config: Config, league_key: str, team: str,
    *, sim_slot: int, sim_speed: int, playoff_weight: float,
):
    """Wire the offline simulation loop: stored settings + scripted picks."""
    from fantasy_coach.draft import DraftLoop, SimulatedPickSource, script_draft, sim_team_names

    settings = store.league_settings(league_key)
    if settings is None:
        console.print(
            f"[bold red]No stored settings for {league_key}.[/] Warm the store "
            "first (simulation runs entirely from the store)."
        )
        raise typer.Exit(code=1)
    num_teams = settings.max_teams or 12
    team_key = team.strip() or f"{league_key}.t.{sim_slot}"
    script = script_draft(store, settings, league_key=league_key)
    console.print(
        f"[dim]Simulating a {num_teams}-team, {len(script) // num_teams}-round "
        f"snake draft ({len(script)} picks); you draft from slot {sim_slot}.[/]"
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
    )


def _build_live_loop(
    store, config: Config, league_key: str, team: str,
    *, warm: bool, playoff_weight: float,
):
    """Wire the live loop: authed Yahoo client, settings, keepers, warm pass."""
    from fantasy_coach.auth.session import get_authed_client
    from fantasy_coach.clients.yahoo import YahooClient
    from fantasy_coach.draft import DraftLoop, YahooPickSource
    from fantasy_coach.ingest.projections import NflverseProjectionSource
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
        result = warm_store(
            store, settings,
            projection_source=NflverseProjectionSource(cache_dir=config.cache_dir),
            schedule=schedule,
            playoff_weight=playoff_weight,
        )
        console.print("[dim]" + result.summary() + "[/]")

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
