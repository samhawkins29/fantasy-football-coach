# Fantasy Coach

A season-long AI Fantasy Football assistant coach for Yahoo Fantasy (NFL-first).
See [`FANTASY_COACH_FRAMEWORK.md`](FANTASY_COACH_FRAMEWORK.md) for the full
vision, architecture, and module roadmap.

> **Status:** **M1 — Auth / OAuth 2.0 + token store** and **M2 — Yahoo Fantasy
> read client** are implemented (framework §5, the pre-draft critical path).
> M3+ (data ingestion + id mapping, projection engine, draft recommender) come
> next and build on the layers shipped here.

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
│       └── clients/               # M2: the Yahoo Fantasy read client
│           ├── __init__.py        # public M2 surface
│           ├── keys.py            # build/split game, league, team, player keys
│           ├── models.py          # typed dataclasses (League, Player, ...)
│           ├── parsers.py         # the one Yahoo-JSON normalization layer
│           ├── throttle.py        # conservative rate limiting (§7)
│           └── yahoo.py           # YahooClient: the typed read surface
└── tests/
    ├── conftest.py                # offline fixtures (httpx.MockTransport, FakeClock)
    ├── fixtures/                  # recorded-shape Yahoo JSON (incl. pagination)
    ├── test_config.py             # config loading + validation
    ├── test_token_store.py        # token model + persistence
    ├── test_oauth.py              # consent URL, code exchange, refresh (mocked HTTP)
    ├── test_session.py            # bearer injection, pre-emptive refresh, 401 retry
    ├── test_cli.py                # CLI smoke tests (no network)
    ├── test_keys.py               # composite-key construction/parsing
    ├── test_throttle.py           # rate limiting + exponential backoff (fake clock)
    ├── test_yahoo_parsers.py      # every parser against the JSON fixtures
    └── test_yahoo_client.py       # pagination, caching, throttling, retries
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
| `python -m fantasy_coach draft --league <key>` | **The live draft companion (M5).** Polls the Yahoo draft room every ~2.5s, rebuilds the drafted set (undo-safe), recomputes the available VORP board with baselines that shift as pools drain, weights it by your unfilled roster slots, and serves an auto-refreshing dark board page at `http://localhost:8787`. Auto-detects your team; seeds keepers from pre-draft rosters. | Polls `draftresults` (throttled) |
| `python -m fantasy_coach draft --simulate` | The identical loop fed by a scripted snake draft generated from the stored board — the full offline dress rehearsal (page, roster fill, recommendations, survival labels). Opponents are profiled bots (market / value-hunter / need-filler / reacher / RB-heavy / zero-RB / QB-early / handcuffer / TE-early / panic drafter) with positional need, run-chasing, reach noise, bye and handcuff logic. `--sim-slot N` picks your slot, `--sim-speed K` reveals K picks per poll, `--sim-seed S` a different (reproducible) room. | None |
| `python -m fantasy_coach setup-league` | **Your league's exact rules, offline.** Reads `data/league.json` (scoring per stat, the full lineup — IDP `D` slot, no kickers, flex-only TE, whatever the league does — playoff weeks, draft length, keeper rules + keepers), stores the settings, and builds the board from the local caches with replacement baselines derived from *this* roster × *this* many teams. No Yahoo needed. `--file` picks another spec. | None (caches) |
| `python -m fantasy_coach refresh` | **The pre-draft freshness pass (step 6).** Re-pulls everything to its most up-to-date state: nflverse projections/schedule/durability caches, current injury statuses from Sleeper (free, no key) and — when authed — Yahoo statuses + ADP; rebuilds the board; prints before/after data vintage. Every step degrades to a warning. `--skip-yahoo` works without auth. | nflverse + Sleeper (+ Yahoo unless skipped) |
| `python -m fantasy_coach vintage` | Shows how fresh every stored data slice is (per-source refresh timestamps), and which sources are truly live vs periodic. | None |

---

## Configuration

Copy `.env.example` to `.env` and fill it in (`.env` is git-ignored):

| Variable | Required? | Meaning |
|---|---|---|
| `YAHOO_CLIENT_ID` | **Yes** | Yahoo app Client ID (Consumer Key). |
| `YAHOO_CLIENT_SECRET` | **Yes** | Yahoo app Client Secret. |
| `YAHOO_REDIRECT_URI` | **Yes** | Registered HTTPS redirect URI. Must match the app exactly. Default `https://localhost:8000/callback`. |
| `YAHOO_SCOPE` | No | Leave **empty** (the default): the authorize URL then carries no `scope` parameter, which is required to get a token the Fantasy API accepts (Yahoo rejects `fspt-r`/empty `scope=` with `invalid_scope`; an `openid` token gets 401 from Fantasy). Set only if you want a narrower non-Fantasy token. |
| `YAHOO_LEAGUE_KEY` | No | League key `{game_key}.l.{league_id}` (e.g. `449.l.123456`). Placeholder for later modules; discover it after login. |
| `FANTASY_COACH_TOKEN_PATH` | No | Where tokens are stored. Default `.tokens.json`. |
| `ODDS_API_KEY` | No | The Odds API key (later modules). |
| `FANTASYPROS_API_KEY` | No | FantasyPros API key (later modules). |
| `FANTASY_COACH_LEAGUE_FILE` | No | Offline league spec path (default `data/league.json`) — see "The league spec" below. |
| `PROJECTION_SOURCE` | No | Which projection source the value engine uses: `nflverse` (free, default — the model in `ingest/projections.py`), `consensus` (blends the nflverse model with market-implied ADP points — see below), or `fantasypros` (needs `FANTASYPROS_API_KEY`). |
| `CONSENSUS_MODEL_WEIGHT` | No | Consensus blend weight for the nflverse model, in [0,1]. Default `0.7`. Weights renormalize over the signals each player actually has. |
| `CONSENSUS_MARKET_WEIGHT` | No | Consensus blend weight for the market/ADP-implied signal, in [0,1]. Default `0.3`. |
| `PLAYOFF_EMPHASIS` | No | Playoff blend weight `w` in [0,1] (step 5): draft value = `(1−w)·season VORP + w·playoff strength`. Default `0` (pure season value). |
| `INJURY_EMPHASIS` | No | Injury/durability discount weight in [0,1] (step 6). Default `0`: risk flags are shown but values/ranks never move; `0.5–1.0` shades Out/IR/Questionable players and chronic games-missers down by the documented, clamped discounts. |
| `RISK_PREFERENCE` | No | Floor↔ceiling tilt in [-1,1]. Every projection carries a floor/ceiling (~20th/80th pct, from historical week-to-week variance + role uncertainty); `0` (default) ranks by the median, `<0` leans on floors (safe), `>0` on ceilings (upside). Only draft value moves. |
| `SOS_EMPHASIS` | No | Per-week strength-of-schedule mix in [0,1]. `0` (default) keeps raw season value; `0.5` values half the season component through each week's own position-specific matchup. Playoff weeks stay weighted heavier by `PLAYOFF_EMPHASIS` on top. |
| `FANTASY_COACH_CACHE_DIR` | No | Local data-cache directory (season projection caches). Default `.cache` (git-ignored). |

### Season projections (free, nflverse-based)

`NflverseProjectionSource` is the default (free) projection source: a simple,
transparent baseline that recency-weights the last three seasons of nflverse
per-game production, regresses low-sample players toward damped positional
priors, and multiplies by regressed projected games. It emits the **raw
component stat line** (`pass_yds`, `pass_td`, `pass_int`, `rush_yds`, `rush_td`,
`rec`, `rec_yds`, `rec_td`, `fum_lost`, `two_pt`, `games`) so the value engine
rescoring uses *your* league's stat modifiers; `points` is only a half-PPR
reference total. Every record is labelled a *model estimate (nflverse-based)*.
Rookies and K/DEF are not covered (no nflverse history / no kicking stat lines)
— the draft board fills those from ADP/market signals.

**Pre-draft warm cache (framework §7):** run
`NflverseProjectionSource().warm_cache()` (or any first `project()` call) while
online — projections are cached per season under `.cache/`, and every later
`project()` is served from disk with zero network. Draft day never depends on a
live nflverse fetch.

### The league spec (`data/league.json`) — the founder's real league

The checked-in spec models Sam's league exactly: **10 teams, full PPR
(1/rec), keeper league**, lineup `QB · 2 RB · 2 WR · 2 W/R/T · DEF · D (any
IDP) · 8 BN`, **no kickers**, TE flex-only, regular season weeks 1–14,
**playoffs weeks 15–17** (top 6, top-2 seeds bye wk 15), no games week 18,
17-round Yahoo snake draft Fri Sep 4 2026 7:15pm ET (1:45/pick), keeper picks
due Sep 1. `setup-league` turns it into `LeagueSettings` indistinguishable
from a live Yahoo pull, so every replacement baseline is derived from that
lineup across 10 teams (e.g. TE replacement sits where the second flex stops
taking TEs; the single IDP slot drains the ten best DL/LB/DB) and kickers are
dropped from the board as unstartable. Everything not specified by the
league is Yahoo's default and is listed under `notes` — confirm against the
Yahoo settings page and edit `scoring` if it differs.

**IDPs** are projected from nflverse's current `stats_player_week` asset
(defensive columns: solo/assist tackles, sacks, INTs, FF/FR, PD, TDs,
safeties, blocks) through the same rate model, with their own positional
priors and floor/ceiling spreads; nflverse sub-positions collapse to Yahoo's
`DL`/`LB`/`DB`, and the `D` slot is a flex over the three. Bots draft IDPs in
the back stretch (one or two per team).

**Keepers.** `keeper_rules` + `keepers` (`{slot: [{player, round}]}`) drive:
kept players leave the pool from pick 1, each one consumes the keeping team's
pick in its cost round (scripted into the simulated draft exactly as Yahoo
pre-populates them; live mode seeds keepers from the pre-draft rosters),
your own altered pick slots are honoured (the loop reads the pick on the
clock as the first *unmade* pick and skips pre-made keeper picks when
predicting your next turns), and the recommendation says whether the pick
on the clock is keeper-eligible next year and at what round cost. Enter
every team's keepers by Sep 1 and re-run `setup-league`. Not yet modelled
(follow-up): valuing *this year's* keeper decisions (which 4 to keep, at what
pick cost) and the round-14/16/17 fill order for multiple undrafted keepers —
the spec takes explicit rounds.

### Projection distributions: floor / median / ceiling

Every nflverse projection now carries a **floor and ceiling** (~20th / 80th
percentile) alongside the median (`ingest/variance.py`). The spread comes from
the player's own **week-to-week fantasy scoring variance** in the history
window (coefficient of variation, shrunk toward a positional prior computed
from qualified players), aggregated to a season total (`cv/√games`), combined
in quadrature with a **role/sample uncertainty** term that grows with how much
of the point projection came from the positional prior (thin history = wide
range). The consensus source carries the spread through and **widens it for
source disagreement** (model vs market-implied). Floors/ceilings ride as
*ratios* so they survive league rescoring; the board brackets both points and
VORP (`floor_vorp` / `ceiling_vorp`), the page shows the range, the
recommendation narrates it ("Floor 189 / median 250 / ceiling 310 — high-
variance, upside bet"). `RISK_PREFERENCE` / `--risk` tilts draft value toward
floor or ceiling; `0` is the identity. Relative uncertainty is the claim, not
calibrated percentiles — the calibration loop is what checks that later.

### Draft survival: "will he last to my next pick?"

`draft/survival.py` estimates, for every available player, the probability
they are still there at your **next** pick and the one **after** — ADP as a
normal distribution (σ grows with ADP; a source stdev wins when present),
conditional on the player still being available now (so a faller is not
written off), shifted by the **room's drift** (median pick − ADP over the last
24 picks) and by **positional runs** (last 8 picks vs the market's expected
positional mix — a run on RB pulls every RB's effective ADP earlier). Players
with no ADP fall back to their rank on the available board with a wide σ. The
loop recomputes it every changed poll. Every player carries a label
(**take now** / coin flip / likely there / **safe to wait**), and the
recommendation runs a **two-pick lookahead**: when a near-equal is unlikely to
survive and the value leader very likely will, it flips to "take X now — Y
should still be there" (and always adds a "Plan: … should still be there at
your next pick" line when a safe runner-up exists). Ranking itself never
changes — survival decides *timing* between near-equals.

### Per-week strength of schedule

`SeasonSchedule.week_multipliers` exposes each player's position-specific
per-week matchup profile (an RB's Week-15 entry is *that* defense's RB points
allowed multiplier). The board values every week through its own opponent
(`sos_vorp`), summarises it as a playoff-weighted `sos_score` (playoff weeks
count 2×), and mixes it into the season component with `SOS_EMPHASIS` /
`--sos-weight` *before* the playoff blend — so the two dials compose: SOS
shades every week, the playoff emphasis re-weights weeks 15–17 on top. The
schedule note now names an extreme playoff-week matchup ("tough wk16 matchup
vs SF (0.78× vs RB)"), and the page shows the wk15–17 strip. `0` is the
identity.

### Consensus projections (opt-in blend — enhancement 1)

Set `PROJECTION_SOURCE=consensus` to blend the nflverse model with a
**market-implied points estimate calibrated from ADP** (Yahoo `draft_analysis`
ADP — already ingested; the field's collective draft wisdom as an independent
signal). The blend fits `points ≈ a + b·ln(adp)` per position on players that
have both signals, then takes a weighted mean
(`CONSENSUS_MODEL_WEIGHT`/`CONSENSUS_MARKET_WEIGHT`, default `0.7/0.3`,
renormalized over whatever signals a player actually has — a single-signal
player passes through untouched, and market-only players keep flowing through
the board's own ADP gap-fill). Records carry an `inputs` label
(`model+market` / `model`) and the store note says *consensus estimate* — a
blend of estimates is still an estimate. A keyed FantasyPros source slots into
the blend via the same `ProjectionSource` protocol with no downstream change.
The consensus caches per season like the model (zero network on draft day), and
any missing source degrades to the rest with a warning. **Off by default:**
leave `PROJECTION_SOURCE` unset and the board is bit-identical to the
single-source nflverse model. (Sleeper exposes no documented projections
endpoint, and ESPN/CBS/NFL scraping is a ToS gray area — neither is used.)

### The data store (SQLite, queryable)

Everything the draft needs lives in **one git-ignored SQLite file** —
`data/coach.sqlite3` (override with `FANTASY_COACH_DB_PATH`): league rules,
canonical players, ADP, projections, weekly stats history, and the computed
value board, each stamped in `data_vintage` with when it was last refreshed.
Warm it once pre-draft, then analyse from a REPL or notebook:

```python
from fantasy_coach.ingest.projections import NflverseProjectionSource
from fantasy_coach.store import CoachStore, warm_store

store = CoachStore()                                  # data/coach.sqlite3
result = warm_store(store, settings,                  # settings from Yahoo (or built offline)
                    projection_source=NflverseProjectionSource(),
                    players=index.players.values())   # M3 crosswalk, optional
print(result.summary())                               # counts + warnings + vintage

store.get_board("449.l.123456", limit=15)             # the stored VORP board
store.top_available("449.l.123456", 10)               # …minus drafted players (M5)
store.adp_vs_vorp("449.l.123456")                     # market-vs-model value gaps
store.player_summary("amon-ra")                       # one player, everything stored
```

Warming is re-runnable (rows upsert; the board snapshot replaces) and
offline-friendly: projections come from the `.cache/` warm cache, and a missing
Yahoo session degrades to warnings while prior rows — including persisted ADP,
which the board's rookie/K/DEF gap-fill needs — survive. And it's *just*
SQLite, so raw SQL works anywhere:

```sql
-- best remaining value by position and tier
SELECT position, tier, COUNT(*) players, ROUND(MAX(vorp), 1) best_vorp
FROM value_board WHERE league_key = '449.l.123456'
GROUP BY position, tier ORDER BY position, tier;

-- players the market drafts later than our model ranks them
SELECT name, position, adp, overall_rank, ROUND(adp - overall_rank, 1) gap
FROM value_board WHERE league_key = '449.l.123456' AND adp IS NOT NULL
ORDER BY gap DESC LIMIT 20;
```

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
   YAHOO_SCOPE=
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

## What M2 does

M2 is a clean, typed **read** client over the Yahoo Fantasy API, built directly
on `AuthedClient`. It deliberately does not delegate to `yfpy` /
`yahoo_fantasy_api` — framework §2.4 wants a raw `httpx` escape hatch that can't
break the week before the draft, and owning the parse layer is what keeps the
whole thing offline-testable.

```python
from fantasy_coach import Config, get_authed_client
from fantasy_coach.clients import YahooClient

client = YahooClient(get_authed_client(Config.load()))

leagues  = client.get_user_leagues(2026)                  # discovery + game_key
settings = client.get_league_settings(leagues[0].league_key)
teams    = client.get_league_teams(leagues[0].league_key)
roster   = client.get_team_roster(teams[0].team_key, week=1)
players  = client.get_players(leagues[0].league_key, status="A",
                              out=("percent_owned", "draft_analysis", "ranks"))
picks    = client.get_draft_results(leagues[0].league_key)  # never cached
```

| Method | Returns |
|---|---|
| `get_game()` / `get_game_key()` | `Game` / the season's numeric `game_key` |
| `get_user_leagues(season=None)` | `list[League]` |
| `get_league(league_key)` | `League` |
| `get_league_settings(league_key)` | `LeagueSettings` (scoring, slots, waivers, playoffs) |
| `get_league_teams(league_key)` | `list[Team]` |
| `get_team_roster(team_key, week=None)` | `TeamRoster` (`.starters` / `.bench`) |
| `get_players(league_key, ...)` | `list[Player]`, paginated 25 at a time |
| `get_draft_results(league_key)` | `list[DraftPick]` |
| `get_transactions(league_key, ...)` | `list[Transaction]` |
| `get_matchups(league_key, week)` | `list[Matchup]` |
| `raw(path, params=None)` | the unmodelled JSON escape hatch |

**Yahoo's JSON is normalized once, in `parsers.py`** (framework §2.3), so no
other module sees its three quirks: collections encoded as `{"0": …, "count": N}`
objects, entity fields split across positional arrays of one-key dicts, and
sub-resources (notably `transaction_data`) that arrive as a bare object in one
leg and a one-element list in the next.

**Rate limiting.** The default client throttles to one request every 2.5s —
framework §7's live-draft-safe rate — so a naive poll loop can't hammer Yahoo.
`429`/`999` responses back off exponentially (2s → 4s → 8s). Every read is warm
cached for 15 minutes **except `get_draft_results`**, which always goes live.

**Player identity for M3.** Every `Player` carries Yahoo's `player_id` *and*
`player_key` plus name/team/position; `player.identity()` projects that to a
`PlayerIdentity`, which is exactly what M3's crosswalk (§3.2) joins on —
`yahoo_id` first, normalized `(name, position, team)` as the fallback.

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
path, and CLI parsing — plus, for M2, every Yahoo parser against recorded-shape
JSON fixtures, player pagination, the warm cache, throttle spacing, and the
`429`/`999` backoff path.

**No test touches the network.** Yahoo responses are served by
`httpx.MockTransport` injected into `AuthedClient`, and the throttle/cache
clocks are faked, so the whole suite runs in well under a second and never
sleeps. You can prove the offline claim by blocking sockets outright:

```bash
python -m pytest -q -p nonet    # with a plugin that raises on socket.connect
```
