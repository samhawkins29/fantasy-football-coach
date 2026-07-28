# Fantasy Coach

A season-long AI Fantasy Football assistant coach for Yahoo Fantasy (NFL-first).
See [`FANTASY_COACH_FRAMEWORK.md`](FANTASY_COACH_FRAMEWORK.md) for the full
vision, architecture, and module roadmap.

> **Status:** **M1 — Auth / OAuth 2.0 + token store** is implemented. This is
> the shared foundation (framework §5, the pre-draft critical path start).
> M2+ (Yahoo client, data ingestion, projection engine, draft recommender) come
> next and build on the auth layer shipped here.

---

## What M1 does

M1 owns the entire Yahoo OAuth token layer — deliberately **not** delegating
token refresh to a wrapper (framework §2.4: "Own the OAuth token layer
yourself"). It provides:

1. **Yahoo OAuth 2.0 authorization-code flow** — build the consent URL, exchange
   the returned `code` for access + refresh tokens.
2. **A local token store** — access + refresh tokens and an absolute expiry
   persisted to `.tokens.json` (git-ignored), with atomic save + load.
3. **Automatic refresh** — the access token is refreshed pre-emptively when it
   is within 5 minutes of expiry, using the refresh token; the store is updated.
4. **An authenticated HTTP client** (`AuthedClient`) that injects the bearer
   token on every request and transparently refreshes on `401`/near-expiry.
   **This is the interface M2 builds on.**
5. **A small CLI** — `login`, `status` (whoami), `logout`, `config`.

> **No live calls in M1.** The code makes exactly one network call in normal
> use — the token exchange you explicitly trigger during `login` — and none at
> all during tests. You supply real Yahoo credentials later (see
> [Authenticate for real](#authenticate-for-real)).

---

## Project structure

```
fantasy-football-coach/
├── FANTASY_COACH_FRAMEWORK.md     # architecture / roadmap (read this)
├── README.md                      # you are here
├── pyproject.toml                 # package metadata + pytest config (src layout)
├── requirements.txt               # runtime + test deps
├── .env.example                   # copy to .env and fill in
├── .gitignore                     # ignores .env, .tokens.json, venvs, caches
├── src/
│   └── fantasy_coach/
│       ├── __init__.py            # re-exports the public auth surface
│       ├── __main__.py            # enables `python -m fantasy_coach`
│       ├── config.py              # env/.env loading -> Config
│       ├── cli.py                 # typer CLI: login / status / logout / config
│       ├── auth/
│       │   ├── __init__.py        # auth public surface
│       │   ├── token_store.py     # Token dataclass + TokenStore (load/save)
│       │   ├── oauth.py           # YahooOAuthClient: consent URL, exchange, refresh
│       │   └── session.py         # AuthedClient + get_authed_client()  <- M2 uses this
│       └── clients/
│           └── __init__.py        # reserved for M2 (Yahoo client)
└── tests/
    ├── conftest.py                # offline fixtures (httpx.MockTransport)
    ├── test_config.py             # config loading + validation
    ├── test_token_store.py        # token model + persistence
    ├── test_oauth.py              # consent URL, code exchange, refresh (mocked HTTP)
    ├── test_session.py            # bearer injection, pre-emptive refresh, 401 retry
    └── test_cli.py                # CLI smoke tests (no network)
```

**Stack** (per framework §2): Python 3.11+, `httpx` (owns the token dance and
the authed session), `requests-oauthlib` (pure consent-URL + CSRF-state
generation, no network), `python-dotenv` (config), `typer` + `rich` (CLI).
`pydantic` is intentionally **not** used in the core — the framework notes a
Windows DLL issue with its Rust core (§2.1), so config is a plain dataclass.

---

## Quick start (no Yahoo account needed)

Everything below runs and passes **without any Yahoo credentials**.

### Windows (PowerShell)

```powershell
cd C:\Users\samha\Desktop\fantasy-football-coach
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .                    # src layout: makes `python -m fantasy_coach` + `import fantasy_coach` work

# run the test suite (all offline)
pytest

# the CLI is now runnable
python -m fantasy_coach --help
python -m fantasy_coach config      # shows which config is set (all "unset" until you add .env)
```

### macOS / Linux

```bash
cd fantasy-football-coach
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
pytest
python -m fantasy_coach --help
```

`pip install -e .` also adds a `fantasy-coach` console script, so you can run
`fantasy-coach --help` instead of `python -m fantasy_coach --help`. (`pytest`
alone works without the editable install — `pyproject.toml` puts `src/` on the
test path — but the CLI needs the package installed.)

---

## CLI commands

| Command | What it does | Network? |
|---|---|---|
| `python -m fantasy_coach login` | Prints the consent URL, opens your browser, then accepts the redirected URL (or bare code) and exchanges it for tokens; saves them to `.tokens.json`. | One call: the token exchange |
| `python -m fantasy_coach status` | Whoami / token status: shows Yahoo GUID, scope, and whether the access token is valid / near-expiry / expired. | None |
| `python -m fantasy_coach logout` | Deletes the stored token file. | None |
| `python -m fantasy_coach config` | Shows which config values are set (secrets masked) and whether OAuth is ready. | None |

---

## Configuration

Copy `.env.example` to `.env` and fill it in (`.env` is git-ignored):

| Variable | Required? | Meaning |
|---|---|---|
| `YAHOO_CLIENT_ID` | **Yes** | Yahoo app Client ID (Consumer Key). |
| `YAHOO_CLIENT_SECRET` | **Yes** | Yahoo app Client Secret. |
| `YAHOO_REDIRECT_URI` | **Yes** | Registered HTTPS redirect URI. Must match the app exactly. Default `https://localhost:8000/callback`. |
| `YAHOO_SCOPE` | No | `fspt-r` (read, default) or `fspt-w` (read/write — set lineups, add/drop). Start read-only. |
| `YAHOO_LEAGUE_KEY` | No | League key `{game_key}.l.{league_id}` (e.g. `449.l.123456`). Placeholder for later modules; discover it after login. |
| `FANTASY_COACH_TOKEN_PATH` | No | Where tokens are stored. Default `.tokens.json`. |
| `ODDS_API_KEY` | No | The Odds API key (later modules). |
| `FANTASYPROS_API_KEY` | No | FantasyPros API key (later modules). |

---

## Authenticate for real

When you're ready to connect a real Yahoo account:

1. **Create a Yahoo Developer app** at <https://developer.yahoo.com/apps/>
   (sign in with the Yahoo account that owns your fantasy team).
   - **Application Type:** *Web Application* (so you get a Client Secret).
   - **Redirect URI (Callback Domain):** `https://localhost:8000/callback`
     (HTTPS required; must match `YAHOO_REDIRECT_URI` exactly).
   - **API Permissions:** enable **Fantasy Sports → Read** to start (upgrade to
     **Read/Write** later when you want the coach to set lineups / make moves).
   - Save and copy the **Client ID (Consumer Key)** and **Client Secret**.

2. **Put them in `.env`:**
   ```
   YAHOO_CLIENT_ID=<your client id>
   YAHOO_CLIENT_SECRET=<your client secret>
   YAHOO_REDIRECT_URI=https://localhost:8000/callback
   YAHOO_SCOPE=fspt-r
   ```
   That is the **minimum** needed to authenticate: `YAHOO_CLIENT_ID`,
   `YAHOO_CLIENT_SECRET`, and a `YAHOO_REDIRECT_URI` that matches the app.

3. **Run the login flow:**
   ```
   python -m fantasy_coach login
   ```
   - It prints a consent URL (and tries to open your browser). Approve access.
   - Yahoo redirects to `https://localhost:8000/callback?code=...`. **That page
     will fail to load — that's expected** (M1 runs no local server). Copy the
     **entire URL** from the address bar and paste it back at the prompt (a bare
     `code` also works).
   - Tokens are exchanged and written to `.tokens.json`. From then on, refresh
     is automatic.

4. **Verify:**
   ```
   python -m fantasy_coach status
   ```

> A future enhancement (noted for M2/M12) is a tiny local HTTPS listener on the
> callback so the code is captured automatically instead of pasted. M1
> intentionally keeps the flow manual and network-light.

---

## The auth interface M2 will consume

M2 (the Yahoo client) should depend only on the authenticated client and never
touch tokens or refresh logic directly:

```python
from fantasy_coach import Config, get_authed_client

config = Config.load()                 # reads .env / environment
client = get_authed_client(config)     # -> AuthedClient (no network yet)

# Every request injects "Authorization: Bearer <token>", refreshes the token
# pre-emptively when near expiry, and retries once on a 401. Relative paths are
# resolved against the Yahoo Fantasy v2 base URL.
resp = client.get("/game/nfl", params={"format": "json"})
resp.raise_for_status()
data = resp.json()
```

Key surface (all in `fantasy_coach.auth`):

- **`get_authed_client(config, *, http=None, oauth_http=None, refresh_leeway=300)`**
  → `AuthedClient`. The one-call entry point. `http` / `oauth_http` accept
  injected `httpx.Client`s (used by the test suite via `httpx.MockTransport`).
- **`AuthedClient`** — `.get/.post/.put/.delete/.request(...)` (bearer-injecting,
  auto-refreshing), plus `.valid_token()`, `.refresh()`, and `.token_status()`.
- **`YahooOAuthClient`** — `.create_authorization_url()`, `.fetch_token(code)`,
  `.refresh_token(refresh_token)`.
- **`TokenStore`** — `.load()`, `.save(token)`, `.clear()`, `.exists()`.
- **`Token`** — dataclass with `.is_expired(leeway=...)`,
  `.seconds_until_expiry()`, `.authorization_header`.

### How token refresh works

- Every token carries an **absolute** `expires_at` (epoch seconds), computed
  from Yahoo's relative `expires_in` at fetch time — so expiry checks never
  depend on when the token was loaded.
- Before each request, `AuthedClient.valid_token()` checks expiry with a
  **5-minute leeway** (`DEFAULT_REFRESH_LEEWAY = 300`). If the token is expired
  or within that window, it calls the refresh grant, saves the new token, and
  proceeds — so a live draft never presents an about-to-die token (framework §7).
- If the server still returns **`401`** (e.g. token revoked server-side), the
  client forces one refresh and retries the request exactly once.
- Yahoo occasionally omits `refresh_token` on a refresh response; the client
  retains the previous refresh token so the store never loses it.
- Refresh + store writes are guarded by a lock, so concurrent requests can't
  trigger duplicate refreshes.

---

## Security notes

- `.env` and `.tokens.json` are git-ignored. **Never commit them.**
- The token file is written atomically with owner-only (`0600`) permissions
  where the OS supports it.
- Tokens are stored as **plaintext on disk**, guarded by filesystem
  permissions. Encryption-at-rest (e.g. OS keyring or a passphrase-derived key)
  is a deliberate later enhancement — the store interface is small enough to
  swap without touching callers.

---

## Testing

```
pytest            # all tests, fully offline
pytest -v         # verbose
```

The suite covers the token store, the expiry/refresh logic (token endpoint
mocked with `httpx.MockTransport`), config loading, the `AuthedClient` 401-retry
path, and CLI parsing. No test touches the network.
