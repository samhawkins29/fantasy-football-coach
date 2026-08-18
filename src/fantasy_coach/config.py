"""Configuration loading for Fantasy Coach.

Secrets and settings are read from the process environment, optionally seeded
from a ``.env`` file via ``python-dotenv``. We deliberately use a plain
``dataclass`` rather than ``pydantic-settings``: the framework (§2.1) notes a
``pydantic`` Rust-core DLL issue on Windows, so the core stays pydantic-free.

Nothing here performs any network I/O.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Yahoo OAuth 2.0 endpoints (stable; see framework §2.3).
YAHOO_AUTHORIZE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

DEFAULT_TOKEN_PATH = ".tokens.json"
# Default OAuth scope is EMPTY on purpose: the Yahoo Fantasy API needs a broad
# token, which Yahoo only grants when the authorize URL carries NO scope
# parameter at all (like yahoo_oauth/yahoofantasy). ``fspt-r`` and an empty
# ``scope=`` both get invalid_scope; an ``openid`` token gets 401
# additional_authorization_required from Fantasy. Empty here means "omit the
# scope parameter entirely" (see YahooOAuthClient.create_authorization_url).
DEFAULT_SCOPE = ""
DEFAULT_PROJECTION_SOURCE = "nflverse"  # free, no key (framework §2.5 free-first)
DEFAULT_CACHE_DIR = ".cache"
DEFAULT_DB_PATH = "data/coach.sqlite3"  # single-file SQLite store (git-ignored)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(slots=True)
class Config:
    """Resolved application configuration.

    Attributes:
        yahoo_client_id: Yahoo app Client ID (a.k.a. Consumer Key).
        yahoo_client_secret: Yahoo app Client Secret.
        yahoo_redirect_uri: Registered HTTPS redirect/callback URI. Must match
            the value configured on the Yahoo Developer app exactly.
        yahoo_scope: OAuth scope. Empty (the default) sends no ``scope``
            parameter, which is required for Fantasy API access — see the
            comment on ``DEFAULT_SCOPE``.
        yahoo_league_key: Optional league key (``{game_key}.l.{league_id}``);
            used by later modules, a placeholder for M1.
        token_path: Path to the local token store JSON file (git-ignored).
        odds_api_key: Optional key for The Odds API (later modules).
        fantasypros_api_key: Optional FantasyPros API key (later modules).
        projection_source: Which ProjectionSource the value engine uses —
            ``"nflverse"`` (free, default), ``"consensus"`` (blends the
            nflverse model with market-implied ADP points — enhancement 1,
            off unless selected), or ``"fantasypros"`` (needs the key).
        consensus_model_weight: Blend weight for the nflverse model when
            ``PROJECTION_SOURCE=consensus`` (default 0.7). Renormalized over
            the signals present per player.
        consensus_market_weight: Blend weight for the market/ADP-implied
            signal in the consensus blend (default 0.3).
        playoff_emphasis: The step-5 playoff blend weight ``w`` in
            ``(1−w)·season VORP + w·playoff strength``. ``0.0`` (default)
            keeps the board pure season value — the schedule layer is
            off-by-default; ``~0.25`` is a meaningful playoff tilt.
        injury_emphasis: The step-6 injury/durability discount weight in
            [0, 1]. ``0.0`` (default) shows risk flags but changes no value
            or rank — off-by-default; ``~0.5–1.0`` applies a half-to-full
            share of the documented status/durability discounts.
        risk_preference: Floor↔ceiling tilt in [-1, 1] (projection
            distributions). ``0.0`` (default) ranks by the median projection;
            negative values lean toward safe floors, positive toward upside
            ceilings (``±0.5`` is a meaningful tilt).
        sos_emphasis: Per-week strength-of-schedule mix in [0, 1]. ``0.0``
            (default) keeps raw season value; ``~0.5`` values every week
            through its own matchup (playoff weeks still weighted heavier
            by ``playoff_emphasis`` on top).
        cache_dir: Local data-cache directory (projections, schedule etc.;
            git-ignored).
        db_path: The single-file SQLite data store (git-ignored via
            ``*.sqlite3``) — league rules, players, ADP, projections, the
            value board.
    """

    yahoo_client_id: str = ""
    yahoo_client_secret: str = ""
    yahoo_redirect_uri: str = "https://localhost:8000/callback"
    yahoo_scope: str = DEFAULT_SCOPE
    yahoo_league_key: str = ""
    token_path: Path = field(default_factory=lambda: Path(DEFAULT_TOKEN_PATH))
    odds_api_key: str = ""
    fantasypros_api_key: str = ""
    projection_source: str = DEFAULT_PROJECTION_SOURCE
    consensus_model_weight: float = 0.7
    consensus_market_weight: float = 0.3
    playoff_emphasis: float = 0.0
    injury_emphasis: float = 0.0
    risk_preference: float = 0.0
    sos_emphasis: float = 0.0
    cache_dir: Path = field(default_factory=lambda: Path(DEFAULT_CACHE_DIR))
    db_path: Path = field(default_factory=lambda: Path(DEFAULT_DB_PATH))

    # -- construction --------------------------------------------------------

    @classmethod
    def load(
        cls,
        env_file: str | os.PathLike[str] | None = ".env",
        *,
        environ: dict[str, str] | None = None,
        load_env: bool = True,
    ) -> "Config":
        """Build a ``Config`` from the environment (and an optional ``.env``).

        Args:
            env_file: Path to a dotenv file to load into the environment before
                reading. Ignored if it does not exist. Pass ``None`` to skip.
            environ: Environment mapping to read from. Defaults to
                ``os.environ``. Handy for tests — no monkeypatching required.
            load_env: When ``True`` (default) and ``environ`` is not provided,
                load ``env_file`` into ``os.environ`` via ``python-dotenv``.

        Returns:
            A populated ``Config``. This does not validate that required OAuth
            fields are present — call :meth:`require_oauth` for that.
        """
        if environ is None:
            if load_env and env_file is not None and Path(env_file).exists():
                # override=False: real environment variables win over .env.
                load_dotenv(env_file, override=False)
            environ = dict(os.environ)

        token_path = environ.get("FANTASY_COACH_TOKEN_PATH", DEFAULT_TOKEN_PATH)

        def emphasis(var: str, default: float = 0.0, lo: float = 0.0) -> float:
            raw = environ.get(var, "").strip()
            try:
                value = float(raw) if raw else default
            except ValueError:
                raise ConfigError(f"{var} must be a number in [{lo:g}, 1], got {raw!r}.")
            if not lo <= value <= 1.0:
                raise ConfigError(f"{var} must be in [{lo:g}, 1], got {value}.")
            return value

        playoff_emphasis = emphasis("PLAYOFF_EMPHASIS")
        injury_emphasis = emphasis("INJURY_EMPHASIS")
        risk_preference = emphasis("RISK_PREFERENCE", lo=-1.0)
        sos_emphasis = emphasis("SOS_EMPHASIS")
        consensus_model_weight = emphasis("CONSENSUS_MODEL_WEIGHT", 0.7)
        consensus_market_weight = emphasis("CONSENSUS_MARKET_WEIGHT", 0.3)

        return cls(
            yahoo_client_id=environ.get("YAHOO_CLIENT_ID", "").strip(),
            yahoo_client_secret=environ.get("YAHOO_CLIENT_SECRET", "").strip(),
            yahoo_redirect_uri=environ.get(
                "YAHOO_REDIRECT_URI", "https://localhost:8000/callback"
            ).strip(),
            # Blank stays blank (= omit the scope param); do NOT coerce to a
            # non-empty fallback — no-scope is the working default for Fantasy.
            yahoo_scope=environ.get("YAHOO_SCOPE", DEFAULT_SCOPE).strip(),
            yahoo_league_key=environ.get("YAHOO_LEAGUE_KEY", "").strip(),
            token_path=Path(token_path),
            odds_api_key=environ.get("ODDS_API_KEY", "").strip(),
            fantasypros_api_key=environ.get("FANTASYPROS_API_KEY", "").strip(),
            projection_source=environ.get("PROJECTION_SOURCE", "").strip().lower()
            or DEFAULT_PROJECTION_SOURCE,
            consensus_model_weight=consensus_model_weight,
            consensus_market_weight=consensus_market_weight,
            playoff_emphasis=playoff_emphasis,
            injury_emphasis=injury_emphasis,
            risk_preference=risk_preference,
            sos_emphasis=sos_emphasis,
            cache_dir=Path(environ.get("FANTASY_COACH_CACHE_DIR", DEFAULT_CACHE_DIR)),
            db_path=Path(environ.get("FANTASY_COACH_DB_PATH", DEFAULT_DB_PATH)),
        )

    # -- validation ----------------------------------------------------------

    @property
    def has_oauth_credentials(self) -> bool:
        """True when the minimum fields to start the OAuth flow are present."""
        return bool(
            self.yahoo_client_id
            and self.yahoo_client_secret
            and self.yahoo_redirect_uri
        )

    def require_oauth(self) -> None:
        """Raise :class:`ConfigError` if OAuth credentials are incomplete.

        Names every missing variable so the user knows exactly what to add to
        their ``.env``.
        """
        missing = [
            name
            for name, value in (
                ("YAHOO_CLIENT_ID", self.yahoo_client_id),
                ("YAHOO_CLIENT_SECRET", self.yahoo_client_secret),
                ("YAHOO_REDIRECT_URI", self.yahoo_redirect_uri),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required Yahoo OAuth config: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill these in "
                "(see README §Setup)."
            )
