# Fantasy Coach — Framework & Architecture

**Project:** Season-long AI Fantasy Football Assistant Coach (Yahoo Fantasy, NFL-primary)
**Status:** v0 — research + architecture. No application code yet.
**Author:** drafted 2026-07-28. Draft target: early September 2026 (~6 week runway).

> **A note on citations & versions.** URLs below are the stable, canonical entry points for each API/library. Exact "latest release" version numbers and dates move constantly — every one is flagged **[verify at setup]** so you `pip index`/check GitHub before pinning. Nothing here should be trusted as a frozen version fact; treat the *URLs and behaviors* as durable and the *version strings* as things to re-confirm.

---

## 1. Vision & Principles

### 1.1 The thesis
The draft is roughly **15% of winning a fantasy league**. The other ~85% is *in-season management*: waivers/FAAB, start/sit, trades, injury reaction, bye planning, and playoff seeding. Most tools are draft-day toys that go dark in Week 1. **Fantasy Coach is a full-season GM/coach.** The draft is *Module One on a shared data layer*, not the product.

### 1.2 Principles
1. **Accuracy through data blending, not a single oracle.** No one projection source is best. We blend weighted consensus + opportunity metrics + Vegas-implied game environment into a *scoring-adjusted* projection tuned to **your** league's exact settings.
2. **League-settings-aware value.** A player's value is a function of *your* scoring (PPR vs half vs standard, TE premium, 6pt pass TD, IDP, superflex, FAAB budget, roster slots). Value must be computed against league settings, never generic.
3. **Everything keyed to a canonical player identity.** All sources join through one crosswalk. No name-joins in the hot path.
4. **Self-improving.** Every week we log *projected vs actual*, score each source's calibration, and reweight the blend. The model that starts the season is not the model that finishes it.
5. **Free-first, paid-where-it-pays.** The core stack is free/open. Paid sources (odds, premium metrics) are optional accelerators behind a clean interface.
6. **Conservative and ToS-respectful.** Polite rate limits, cache aggressively, prefer official APIs and openly-licensed data over fragile scrapes. Live-draft polling is deliberately slow (~1 req / 2–3 s).
7. **Sport-flexible core.** The data layer, id-mapping, and value engine are abstracted so NBA/MLB/NHL can be added as adapters. NFL is the first (and reference) implementation.

---

## 2. Recommended Tech Stack

### 2.1 Language & runtime
- **Python 3.11+**. Entire fantasy/NFL data ecosystem is Python-native.
- **Windows note:** you hit a `pydantic` DLL issue on another project (see stormwater-agent memory). Prefer a clean venv per project; if pydantic v2's Rust core misbehaves on Windows, pin a known-good build or use the `pydantic` pure-python fallback path. Keep pydantic optional in the core (dataclasses work).

### 2.2 Core libraries
| Concern | Choice | Why |
|---|---|---|
| Data wrangling | `pandas` (+ `pyarrow`) | nflverse returns parquet/DataFrames |
| HTTP | `httpx` or `requests` | httpx gives async for parallel feed pulls |
| OAuth2 | `requests-oauthlib` or `authlib` | generic, well-maintained; owns the Yahoo token dance |
| Yahoo read/write | see **§2.4** | library-vs-raw decision |
| Storage | **SQLite** (via `sqlite-utils`/SQLAlchemy) first; Postgres later | one-file, zero-ops; season data is small |
| Scheduling | APScheduler (in-season jobs) | cron-like waiver/injury polling |
| Fuzzy match | `rapidfuzz` | fast, MIT, for id-mapping fallback |
| CLI/UX | `typer` + `rich` | fast to build, great tables for draft board |
| Config/secrets | `pydantic-settings` or plain `.env` + `python-dotenv` | keep tokens out of code |

### 2.3 Yahoo Fantasy API — the primary integration

Canonical docs: **https://developer.yahoo.com/fantasysports/guide/** · App registration: **https://developer.yahoo.com/apps/**

**OAuth 2.0 (authorization code grant).**
- Register an app at developer.yahoo.com/apps to get a **Client ID (consumer key)** and **Client Secret**. Request **Fantasy Sports → Read** (`fspt-r`) or **Read/Write** (`fspt-w`) permission. For a coach that eventually *sets lineups and makes add/drops for you*, you want **`fspt-w`**; if you only ever advise, `fspt-r` is enough. Start read-only, upgrade later.
- Endpoints:
  - Authorize: `https://api.login.yahoo.com/oauth2/request_auth`
  - Token / refresh: `https://api.login.yahoo.com/oauth2/get_token`
- **Redirect URI:** register an HTTPS redirect (e.g. `https://localhost:8000/callback`) and run a tiny local listener to catch the `?code=`. Yahoo historically supported an out-of-band `oob` flow (copy/paste the code) — treat `oob` as **deprecated/unreliable in 2026** and use a real redirect URI. **[verify at setup]**
- **Tokens:** access token lifetime **~1 hour (3600s)**. You get a long-lived **refresh token**; refresh silently before expiry. Persist both in an encrypted local token store (see M1). This is the #1 operational gotcha — during a live draft you must refresh transparently or you'll drop the connection mid-round.

**Base API:** `https://fantasysports.yahooapis.com/fantasy/v2/`
Add **`?format=json`** to everything (default is XML). Yahoo's JSON is notoriously awkward — deeply nested, and **arrays are encoded as objects with numeric string keys plus a `count`**. Write one normalization layer that flattens this once, so the rest of the app never sees it.

**Key formats (memorize these):**
- `game_key` — per-sport, per-season integer/code (NFL changes every year, e.g. `nfl` alias or a numeric like `449`). Discover the current season's key via `/game/nfl` (the alias `nfl` resolves to the current in-season game). **[verify current-season numeric key at setup]**
- `league_key` = `{game_key}.l.{league_id}` → e.g. `449.l.123456`
- `team_key` = `{league_key}.t.{team_id}` → e.g. `449.l.123456.t.4`
- `player_key` = `{game_key}.p.{player_id}` → e.g. `449.p.31883`

**Endpoints you'll actually use:**
| Need | Resource | Example |
|---|---|---|
| Discover current NFL game | `/game/nfl` | `…/fantasy/v2/game/nfl?format=json` |
| Your leagues | `/users;use_login=1/games;game_keys=nfl/leagues` | list the user's leagues |
| League settings + **scoring** | `/league/{league_key}/settings` | roster slots, scoring rules, FAAB, waiver type |
| Standings / scoreboard | `/league/{league_key}/standings`, `/scoreboard;week=N` | matchups |
| Your roster | `/team/{team_key}/roster;week=N` | start/sit state |
| Player pool + stats | `/league/{league_key}/players;start=0;count=25;status=A` | paginate 25/page |
| Player ownership | `/league/{league_key}/players;player_keys=…/percent_owned` | waiver signal |
| Draft results | `/league/{league_key}/draftresults` | live + post draft |
| Transactions | `/league/{league_key}/transactions` | adds/drops/trades/FAAB bids |

Sub-resource selectors chain with `/` and filters use `;key=value` (e.g. `players;position=RB;status=A;sort=AR`). **Player queries paginate at 25 results** — loop `start=0,25,50,…`.

**Rate limits.** Yahoo does **not** publish a hard official number; the community-observed ceiling is on the order of a few thousand requests/hour/app, with throttling/`999`-style errors if you burst. **Design as if you have ~1 req/sec sustained budget**, cache everything, and for live draft polling stay at **1 request every 2–3 seconds** (see §7). **[verify — undocumented]**

### 2.4 yfpy vs yahoo_fantasy_api — honest maintenance verdict

Two mature community wrappers exist. Both wrap the same REST API above; the question is what they save you and whether they're alive in 2026.

| | **yfpy** | **yahoo_fantasy_api** |
|---|---|---|
| Repo | github.com/uberfastman/yfpy | github.com/spilchen/yahoo_fantasy_api |
| PyPI | pypi.org/project/yfpy | pypi.org/project/yahoo-fantasy-api |
| OAuth | Built-in browser flow + token JSON persistence | Relies on companion **`yahoo_oauth`** (github.com/josuebrunel/yahoo-oauth) |
| Strength | **Richest read models** — clean typed objects for leagues, rosters, players, draftresults, transactions, matchups. Best for *ingesting everything*. | **Read + write** — supports `add`, `drop`, `add_and_drop`, and **`change_positions`/set lineup**. Best for *taking actions*. |
| Maintenance (2026) | **Actively maintained** — regular releases, responsive issues; the healthier of the two. **[verify latest release date at setup]** | **Sporadically maintained** — functional and widely used, but slower cadence; `yahoo_oauth` in particular is old and lightly maintained. **[verify]** |
| Weakness | Read-focused (writes are not its purpose) | Thinner data models; leans on you to parse; oauth dep is the stale link |

**Verdict / recommendation:**
- **Use `yfpy` as the primary ingestion client** for all *reads* (settings, players, rosters, draftresults, transactions). It has the best-maintained, richest data models and its own OAuth/token persistence — exactly what a data-heavy season-long tool wants.
- **Use `yahoo_fantasy_api` (or a thin raw-`requests` writer) only for the write actions** (set lineup, add/drop, FAAB bid) once you enable `fspt-w`. Keep writes behind a small interface so the dependency is swappable.
- **Own the OAuth token layer yourself** with `requests-oauthlib`/`authlib` rather than fully trusting `yahoo_oauth`'s staleness. Both wrappers can consume a token file you control. This de-risks the single most fragile dependency (mid-draft token refresh).
- **Escape hatch:** because Yahoo's REST surface is small and stable, keep a raw `httpx` client that can hit any endpoint directly. If a wrapper breaks the week before your draft, you are not blocked. Treat the wrappers as conveniences, not load-bearing.

### 2.5 External data sources — the "all the data" stack

Free-first. Everything flows through the crosswalk (§3) and is cached locally.

| Layer | Source | Access | API/Path | ToS / caution |
|---|---|---|---|---|
| **Core NFL stats** (pbp, weekly, snaps, rosters, depth charts, schedules, injuries, **IDs**) | **nflverse** via **`nfl_data_py`** / `nflreadr` | **Free, open** | github.com/nflverse/nfl_data_py — `import_weekly_data`, `import_snap_counts`, `import_seasonal_rosters`, `import_depth_charts`, `import_schedules`, `import_injuries`, `import_ids` | Community data (CC-ish). Cite nflverse. **The backbone.** |
| **Advanced/opportunity** (target share, air yards, aDOT, RZ touches, routes) | Derived from **nflverse pbp** (+ `ffopportunity` expected-points) | **Free** | Compute from `import_pbp_data`; DynastyProcess `ffopportunity` for expected fantasy pts | Free/open |
| **Consensus projections & ECR/ADP** | **FantasyPros** | Free to read on-site; **official API is paid/partner** | fantasypros.com/api (partner key) or careful scrape of public ECR/ADP pages | **Scraping = ToS gray area / no redistribution.** Prefer their API tier or personal-use-only, low volume. |
| **ADP (alt/free)** | **Fantasy Football Calculator**, **Sleeper ADP**, **Underdog** | Free | FFC has a lightweight ADP JSON; Sleeper API exposes ADP-ish drafts data | Personal use; attribute |
| **Rankings/news/ADP + IDs** | **Sleeper API** | **Free, no key** | docs.sleeper.com — `players/nfl` (carries cross-platform IDs incl. yahoo_id), trending adds/drops, injuries | Generous but be polite; cache the big players blob daily |
| **Undocumented rich stats** | **ESPN hidden API** | Free, unofficial | `site.api.espn.com/...` and `fantasy.espn.com/apis/v3` | Undocumented — can change without notice; use as *supplement*, not backbone |
| **Vegas odds / implied team totals** | **The Odds API** | **Free tier (~500 req/mo)**; paid tiers scale | the-odds-api.com — `/v4/sports/americanfootball_nfl/odds` (markets: `spreads`, `totals`) | Clean ToS with a key. Derive **implied team total = total/2 ± spread/2**. |
| **Weather** | **Open-Meteo** (primary) / **NWS api.weather.gov** | **Free** | open-meteo.com (no key, generous), weather.gov (US gov, free, needs User-Agent) | Open-Meteo free for non-commercial; NWS public domain. Map each stadium→lat/lon; flag **domes** as neutral. |
| **Injury/news (timely)** | **Sleeper** (status), **nflverse injuries**, ESPN news, RotoWire/RotoBaller feeds | Free (Sleeper/nflverse); some paid | Poll Sleeper trending + injuries; ESPN news endpoint | Respect source ToS; news text is for signal, don't republish |
| **Premium (optional, paid)** | **PFF**, **FantasyPoints**, **4for4**, **Establish The Run** | Paid subscription | Mostly site-gated; some CSV export | Only add behind the source interface; big accuracy lift for grades/routes but not required |

**Recommended concrete stack (free-first):**
- **Backbone:** `nfl_data_py`/nflverse (stats, snaps, schedules, depth charts, injuries, **and the id map**).
- **Projections/ADP:** blend **FantasyPros** (via API if you get a key; otherwise low-volume personal scrape) with **free ADP** from Sleeper/FFC as a sanity check.
- **Game environment:** **The Odds API** (free tier) for totals/spreads → implied team totals; **Open-Meteo** for weather.
- **News/status:** **Sleeper** (free, no key) for injuries + trending adds (great waiver signal).
- **Add paid later** (PFF/FantasyPoints) only if you want grades/route data; wire behind the same interface so the engine doesn't care where a number came from.

---

## 3. Unified Data Model & ID Mapping

### 3.1 The canonical player identity
Joining sources on **name is unreliable**: `Jr./Sr./II/III` suffixes, `D.J.` vs `DJ`, apostrophes/accents (`Amon-Ra`, `Ken Walker`), mid-season trades (team changes), rookies not yet in every DB, kickers, and **team defenses (DST)** which aren't "players" at all. We never join on name in the hot path.

**Hub-and-spoke crosswalk.** Pick one **canonical hub id** and map every source's native id to it:
- **Recommended hub: `gsis_id`** (NFL's official player id, used throughout nflverse) for real players, because our stat backbone is nflverse. Keep **`mfl_id`** and **`sleeper_id`** as strong secondary anchors.
- **Primary crosswalk source:** the **nflverse / DynastyProcess player-id map** — `nfl_data_py.import_ids()` (backed by **DynastyProcess `db_playerids.csv`**, github.com/dynastyprocess/data). It maps, per player, a wide set of columns: `gsis_id`, `pfr_id`, `sleeper_id`, `espn_id`, `yahoo_id`, `fantasypros_id`, `rotowire_id`, `sportradar_id`, `mfl_id`, `pff_id`, `cbs_id`, name/pos/team/birthdate. This one table is what makes "all the data" joinable.
- **Second yahoo source:** **Sleeper's `players/nfl`** object also carries a `yahoo_id` (and gsis/espn/rotowire ids) per player — use it to **fill gaps** where DynastyProcess lacks a Yahoo mapping.

**Coverage reality:** Yahoo ids in these crosswalks are *good but not 100%*, and lag for **rookies** and just-signed players early in the season. So the mapping is a pipeline, not a single lookup.

### 3.2 The id-mapping pipeline
1. **Load master crosswalk** (`import_ids()` + Sleeper `yahoo_id`) → table keyed by `gsis_id` with all spoke ids.
2. **Direct id join** wherever a source already exposes a mapped id (the happy path — Yahoo `player_id` → `gsis_id`).
3. **Deterministic fallback** for unmatched: normalize `(clean_name, position, team)` — strip suffixes/punctuation, casefold, unaccent — and exact-match on that tuple.
4. **Fuzzy fallback** with **`rapidfuzz`** on normalized name **within (position, team)** buckets, above a confidence threshold; anything below threshold goes to a review queue.
5. **Manual override table** (`overrides.csv`, hand-curated) — highest priority, wins over everything. This is where you fix the rookie/trade edge cases once and forever.
6. **DST / team defenses:** handle separately — map by **team code** (e.g. `SF`, `NYJ`) to a synthetic canonical id like `DST_SF`. Kickers follow the normal player path.
7. **Refresh cadence:** rebuild the crosswalk **daily in-season** (rookies/signings churn), cache to SQLite, and log unmatched players so coverage improves over the year.

### 3.3 Canonical player record (shape)
```
CanonicalPlayer
  canonical_id        # gsis_id (or synthetic DST_/rookie id)
  ids: {yahoo, sleeper, espn, pfr, fantasypros, rotowire, sportradar, mfl, pff}
  name, clean_name, position, team, bye_week
  status              # active/IR/questionable/out/doubtful (from injuries feed)
  depth_chart_rank
  # attached, not stored on the identity:
  projections{source -> points}, blended_projection
  opportunity{snap_pct, target_share, air_yards_share, rz_touches, routes}
  environment{implied_team_total, spread, weather, dome}
  market{adp, ecr, percent_owned, percent_rostered, trend}
  value{vorp, tier, scoring_adjusted_points}
```

### 3.4 League settings drive value
Ingest `/league/{key}/settings` once and derive a **`ScoringProfile`**: point-per-stat map, roster slots (QB/RB/WR/TE/FLEX/K/DST/superflex/IDP), bench/IR size, **PPR type**, TE premium, waiver type + **FAAB budget**, playoff weeks. **Every projection is recomputed into this profile's points** before any value math. Two managers in different leagues get different rankings from the same raw stats — that's the point.

---

## 4. The Accuracy Engine

Pipeline: **raw stats → scoring-adjusted projections (blended) → value (VORP/VBD, tiers) → draft survival & in-season decisions → weekly calibration that reweights the blend.**

### 4.1 Scoring-adjusted, blended projections
For each player *p* and week/season horizon:

1. **Convert every source's stat line to *your* league points** using the `ScoringProfile` (never use a source's own pre-scored number if you can rescore its underlying stats — that removes scoring mismatch).
2. **Blend sources with a weighted mean** (weights start from priors, then get learned by §4.5):
   `proj(p) = Σ_s w_s · points_s(p) / Σ_s w_s`
   Sources *s* = FantasyPros consensus, nflverse-derived opportunity model, any premium projections.
3. **Opportunity adjustment.** Nudge toward **usage** (snap %, target share, air-yards share, RZ touches, routes run) via an expected-points model (`ffopportunity`-style). Volume is stickier and more predictive than past fantasy points — weight it.
4. **Game-environment adjustment.** Scale by **Vegas implied team total** (offense in a 27.5-implied game > same player in a 17-implied game) and apply **weather** penalties (wind/precip hurt passing & kicking; domes neutral).
5. **Uncertainty.** Carry a **projection distribution** (mean + variance / floor–ceiling), not just a point estimate. Ceiling matters for tournaments/underdog weeks; floor matters when you're favored. Estimate variance from source disagreement + role volatility.

### 4.2 Draft value — VORP / VBD, tiers, tier cliffs
- **VORP/VBD (Value Over Replacement Player):** value = projected points **minus the baseline points of a replacement-level player at that position**, given league size and starting slots. Baseline = the projected points of the *last startable* player at the position (e.g. in a 12-team league starting 2RB+FLEX, the ~30th–36th RB). This is what makes cross-position comparison (should I take the RB or the WR?) valid.
  `VORP(p) = proj(p) − baseline(pos, league_size, starters)`
- **Dynamic baselines:** recompute baselines from *your* roster slots (superflex changes QB baselines dramatically).
- **Tiers & tier cliffs:** cluster players within a position by projected value (gap-based or k-means on VORP) to form **tiers**. A **tier cliff** = a large VORP drop between adjacent players. The draft rule: *when the last player in a tier is about to be gone and the next tier is a cliff below, that's your pick pressure* — reach for the cliff, not the raw ranking.

### 4.3 ADP-survival ("will he last to my next pick?")
The draft optimizer isn't "take the best player" — it's **"take the best player who won't survive to my next pick, and let the safe ones fall."**

- Model each player's **draft-survival probability** to your next pick using **ADP as a distribution**, not a point. Given a player's ADP mean μ and spread σ (from ADP source dispersion), and the number of picks *k* until you're up again, estimate `P(available at my next pick)`. A simple, robust form: treat each intervening pick as a draw and model `P(taken by pick n)` with a logistic/normal CDF around ADP.
- **Combine value × scarcity:** rank by *expected value lost if you wait* = `VORP(p) · P(gone before next pick)`. This surfaces the player you must take **now** vs. the equal-value player likely to fall.
- Feed **live draftresults** in (Module 5) so survival updates in real time as the run on a position accelerates.

### 4.4 In-season decision models (same engine, different objective)
- **Start/Sit (M6):** maximize expected lineup points subject to roster-slot constraints — an assignment/optimization over blended weekly projections; use **floor** when favored, **ceiling** when you need a blow-up. FLEX and superflex handled by the optimizer, not by hand.
- **Waiver/FAAB (M7):** rank available players by *marginal VORP added to your roster* (not raw value), weight by **trend** (Sleeper trending adds, snap/target upticks), and convert to a **FAAB bid** as a fraction of remaining budget scaled by need and scarcity. Recommend bid *ranges*.
- **Trade analyzer (M8):** value both sides with **rest-of-season VORP** (not season-total), adjust for **your roster construction** (positional surplus/need), bye conflicts, and schedule; flag win-win vs. fleece and give a confidence.
- **Playoff planner (M10):** re-weight rest-of-season value toward **your fantasy playoff weeks** using **strength-of-schedule** (opponent defense vs. position) and byes.

### 4.5 Self-improving weekly calibration loop
This is the flywheel that makes accuracy compound over the season.

1. **Log** every pre-week blended projection per player (and each *source's* projection) to a `projections_history` table.
2. **After each week, join to actuals** (nflverse weekly / Yahoo stats) → per-source error (MAE, bias, and calibration of the floor/ceiling intervals).
3. **Reweight the blend:** update each source's weight `w_s` inversely to its recent error (e.g. exponentially-weighted so recent weeks matter more; or a simple online ridge/stacking regression that learns optimal source weights per position). Track weights *per position* — a source great at WR may be poor at TE.
4. **Recalibrate uncertainty:** if 80% intervals only cover 60% of outcomes, widen them.
5. **Detect drift:** flag players where projection error is structurally high (role change, injury return) for manual attention.
6. **Persist** the learned weights so next season starts from a better prior. Over the year the coach literally gets more accurate at *your* league.

---

## 5. Module Breakdown (MVP-first)

Legend: **[PRE-DRAFT]** must ship before early-Sept draft · **[IN-SEASON]** needed Week 1+ · **[CROSS]** shared infra.

| # | Module | When | Depends on | Summary |
|---|---|---|---|---|
| **M1** | **Auth / OAuth + token store** | **[PRE-DRAFT] [CROSS]** | — | Yahoo OAuth2 code flow, encrypted token persistence, silent auto-refresh. *Nothing works without this.* |
| **M2** | **Yahoo client** | **[PRE-DRAFT] [CROSS]** | M1 | Read wrapper (yfpy) + raw httpx escape hatch + JSON-normalization layer; pull leagues, **settings/scoring**, rosters, players, draftresults, transactions. |
| **M3** | **Data ingestion + ID mapping + external feeds** | **[PRE-DRAFT] [CROSS]** | M2 | nflverse pull, crosswalk build (§3), FantasyPros/ADP, Odds API, weather, Sleeper. The shared data layer. |
| **M4** | **Projection / value engine** | **[PRE-DRAFT] [CROSS]** | M3 | Scoring-adjusted blended projections, VORP/VBD, tiers/cliffs, uncertainty. §4.1–4.2. |
| **M5** | **Draft monitor + recommender** | **[PRE-DRAFT]** ⭐ | M4 | Live `draftresults` polling, ADP-survival (§4.3), value×scarcity board, tier-cliff alerts, pick recommendations. **The Sept deliverable.** |
| **M6** | **Start/Sit optimizer** | **[IN-SEASON]** | M4 | Weekly lineup optimization, floor/ceiling modes, FLEX/superflex. |
| **M7** | **Waiver / FAAB engine** | **[IN-SEASON]** | M4, trends | Marginal-VORP waiver ranking + FAAB bid recommendations. |
| **M8** | **Trade analyzer** | **[IN-SEASON]** | M4 | Rest-of-season VORP both sides, roster-fit, fairness + confidence. |
| **M9** | **Injury / news monitor** | **[IN-SEASON]** | M3 | Poll injuries/status/trending; alerting; feeds M6/M7. |
| **M10** | **Playoff planner** | **[IN-SEASON]** (late) | M4, SOS | Rest-of-season & playoff-week SOS, byes, seeding. |
| **M11** | **Self-improving calibration** | **[IN-SEASON]** | M4 logging | Weekly projected-vs-actual, reweight sources, recalibrate intervals (§4.5). |
| **M12** | **CLI / UX** | **[CROSS]** (draft view **[PRE-DRAFT]**) | all | `typer`+`rich` draft board, roster views, weekly report. Draft board must exist for M5. |

**Critical path to the draft:** **M1 → M2 → M3 → M4 → M5** (+ a minimal M12 draft board). Everything else is in-season and can land after Week 1.
**Build order recommendation:** M1, M2, M3 (with id-mapping), M4, then M5 + draft CLI. Aim to have M1–M4 solid ~2 weeks before the draft so you can dry-run M5 against a mock draft.

---

## 6. Setup Guide (for the user)

### 6.1 Create your Yahoo Developer app
1. Go to **https://developer.yahoo.com/apps/** and sign in with the Yahoo account that owns your fantasy team.
2. **Create an App.**
   - **Application Type:** Web Application (so you get a Client Secret).
   - **Redirect URI (Callback Domain):** `https://localhost:8000/callback` (we'll run a tiny local listener). Must be HTTPS.
   - **API Permissions:** enable **Fantasy Sports**, choose **Read** to start (upgrade to **Read/Write** later when you want the coach to set lineups / make moves).
3. Save. Copy your **Client ID (Consumer Key)** and **Client Secret**. Treat these like passwords.

### 6.2 Find your league key
- After OAuth, hit `/users;use_login=1/games;game_keys=nfl/leagues?format=json` to list your leagues and read the `league_key` (looks like `449.l.123456`). We'll add a helper to print it. You'll paste it into config.

### 6.3 External accounts / keys (all optional except nflverse, which needs none)
| Source | Needed? | How |
|---|---|---|
| **nflverse / nfl_data_py** | Required, **no key** | `pip install nfl_data_py` |
| **Sleeper API** | Recommended, **no key** | just HTTP |
| **The Odds API** | Recommended for Vegas | free key at the-odds-api.com (free tier ~500 req/mo — plenty with caching) |
| **Open-Meteo weather** | Recommended, **no key** | just HTTP |
| **FantasyPros API** | Optional (paid/partner) | request an API key; else we use free ADP sources |
| **PFF / FantasyPoints** | Optional (paid) | only if you want premium grades/routes |

### 6.4 Config / secrets
Create a `.env` (git-ignored) — **never commit secrets**:
```
YAHOO_CLIENT_ID=...
YAHOO_CLIENT_SECRET=...
YAHOO_REDIRECT_URI=https://localhost:8000/callback
YAHOO_LEAGUE_KEY=449.l.123456
ODDS_API_KEY=...            # optional
FANTASYPROS_API_KEY=...     # optional
```
First run launches the browser OAuth consent; the app captures the code on the local callback, exchanges it for tokens, and writes `token.json` (also git-ignored). After that, refresh is automatic.

---

## 7. Rate-Limit & Live-Draft Polling Strategy

**Principle: be conservative and cache.** Yahoo's limits are undocumented; getting throttled mid-draft is catastrophic, so we run well under any plausible ceiling.

- **Live draft polling: 1 request every 2–3 seconds** to `/league/{key}/draftresults` (and occasionally `/settings` once at start). At ~1 pick / 30–90 s in a real draft, a 2–3 s poll is more than fast enough and stays at ~0.3–0.5 req/s. **[verify — undocumented limit]**
- **Adaptive backoff:** on any `999`/throttle/HTTP 429, exponential backoff (2s→4s→8s…), surface a visible "slowed by Yahoo" state, and never hammer.
- **Diff, don't re-pull:** track the last-seen pick count; only recompute the board when new picks appear.
- **Token pre-refresh:** refresh the access token if it expires within ~5 min *before* the draft starts and on a timer during — never let a 401 interrupt a pick.
- **Pre-draft warm cache:** pull all static data (players, projections, ADP, crosswalk, settings) **before** the draft. During the draft, the *only* live Yahoo call is draftresults. Everything else (value, survival, tiers) is computed locally from the warm cache. This keeps live API pressure minimal and the recommender instant.
- **In-season polling (much slower):** waivers/injuries/news on schedules (e.g. injuries every 30–60 min on game days, hourly otherwise; transactions a few times/day). Batch and cache; respect each external source's own limits (Sleeper: cache the big players blob daily; Odds API: pull totals a few times/week to stay in free tier).

---

## 8. Open Questions / To Verify at Build Time
- Exact **current NFL `game_key`** for the 2026 season (`/game/nfl` resolves it live). **[verify]**
- Latest **yfpy** and **yahoo_fantasy_api** release dates/versions before pinning. **[verify]**
- Whether **`oob`** redirect still works (use a real HTTPS redirect regardless). **[verify]**
- **Yahoo `yahoo_id` coverage** for 2026 rookies in the DynastyProcess/Sleeper crosswalks early in the season — expect gaps; lean on the override table. **[verify]**
- **FantasyPros API** access/cost for your account tier vs. free ADP fallback. **[verify]**

---

### Source URLs (canonical entry points)
- Yahoo Fantasy API guide — https://developer.yahoo.com/fantasysports/guide/
- Yahoo app registration — https://developer.yahoo.com/apps/
- Yahoo OAuth2 — https://developer.yahoo.com/oauth2/guide/
- yfpy — https://github.com/uberfastman/yfpy · https://pypi.org/project/yfpy/
- yahoo_fantasy_api — https://github.com/spilchen/yahoo_fantasy_api · yahoo_oauth — https://github.com/josuebrunel/yahoo-oauth
- nfl_data_py / nflverse — https://github.com/nflverse/nfl_data_py · https://github.com/nflverse
- DynastyProcess player-id data — https://github.com/dynastyprocess/data
- Sleeper API docs — https://docs.sleeper.com/
- The Odds API — https://the-odds-api.com/
- Open-Meteo — https://open-meteo.com/ · NWS — https://www.weather.gov/documentation/services-web-api
- FantasyPros — https://www.fantasypros.com/ (API is partner/paid)
- rapidfuzz — https://github.com/rapidfuzz/RapidFuzz

*Version strings and undocumented limits above are flagged **[verify at setup]** — confirm before pinning. This is an architecture doc; no application code has been written yet.*
