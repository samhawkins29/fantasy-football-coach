"""Command-line interface for Fantasy Coach auth (M1).

Commands:
    login    Run the Yahoo OAuth consent flow and store tokens.
    status   Show the stored token's state (a.k.a. whoami). No network.
    logout   Delete the stored tokens.
    config   Show which config values are set (secrets masked). No network.

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
    help="Fantasy Coach — Yahoo auth (M1). Manage OAuth login and tokens.",
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
