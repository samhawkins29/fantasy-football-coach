# Draft-Day Runbook

Everything runs from the **project root** (paths like `data/coach.sqlite3` are
relative to where you run the command) using the project venv:

```powershell
cd C:\Users\samha\Desktop\fantasy-football-coach
.venv\Scripts\python.exe -m fantasy_coach <command>
```

(One-time setup, already done on this machine: `python -m venv .venv`,
`.venv\Scripts\pip install -e .[dev]`, Yahoo app creds in `.env`.)

## 1. Once, when your league exists (any time pre-draft)

```powershell
.venv\Scripts\python.exe -m fantasy_coach login        # Yahoo OAuth; paste the redirect URL back
.venv\Scripts\python.exe -m fantasy_coach status       # confirm token valid + GUID shown
.venv\Scripts\python.exe -m fantasy_coach refresh --league <game>.l.<id>
```

That `refresh` (authed) pulls league settings, **crosswalks the Yahoo player
universe to canonical ids** (this is what lets live picks resolve — don't skip
it), plus projections / schedule / durability / Sleeper + Yahoo statuses / ADP,
and builds the value board. Set `YAHOO_LEAGUE_KEY=<game>.l.<id>` in `.env` so
you can drop `--league` everywhere.

## 1b. Your league's rules (once) and keepers (by Sep 1)

Keepers can go into `data/league.json` (`"keepers": {"3": [{"player": "Puka
Nacua", "last_round": 9}]}` — cost round derived: 9 → 6; `"undrafted"` → 15)
**or** be entered on the draft page's *Keepers* panel once `draft --manual`
is up (team → player → "drafted last year in round …" → Add). Both land in
the store; the page shows every team's keepers with their cost round.

```powershell
# data/league.json holds the league exactly: 10 teams, full PPR, QB/2RB/2WR/2FLEX/DEF/D(IDP)/8BN,
# no K, playoffs wk15-17, 17 rounds, keeper rules. Fill draft.my_slot + keepers when known.
.venv\Scripts\python.exe -m fantasy_coach setup-league     # stores settings, builds the board (offline)
.venv\Scripts\python.exe -m fantasy_coach refresh --skip-yahoo   # latest nflverse/Sleeper, IDP included
```

Check the printed replacement baselines (QB/RB/WR/TE/LB/DL/DB) and the
`notes` — everything not confirmed is Yahoo default scoring; edit and re-run.

## 2. The week before: dress rehearsal

```powershell
.venv\Scripts\python.exe -m fantasy_coach vintage      # every slice should be recent
.venv\Scripts\python.exe -m fantasy_coach draft --simulate --sim-slot <your slot> --playoff-weight 0.3 --injury-weight 0.5 --sos-weight 0.5 --risk 0.2
```

Offline replay of a full snake draft through the identical live loop at
http://localhost:8787, against a room of profiled bots (market drafters, value
hunters, need-fillers, reachers, RB-heavy / zero-RB / QB-early archetypes,
handcuffers, panic drafters — they chase runs and reach like real rooms;
`--sim-seed N` for a different room). Practice reading the board: BEST PICK
NOW, the floor–ceiling range, the **Avail** column (P still there at your next
pick: take now / coin flip / likely / safe), the "Plan:" line, tier cliffs,
injury badges, the wk15–17 matchup strip, the room footer (runs + drift), the
vintage footer.

## 3. Draft day (morning)

```powershell
.venv\Scripts\python.exe -m fantasy_coach status       # token still good? if not: login
.venv\Scripts\python.exe -m fantasy_coach refresh      # latest injuries/ADP; check the After table
```

## 4. Draft day (when the room opens)

**Without Yahoo API access (the default plan):**

```powershell
.venv\Scripts\python.exe -m fantasy_coach draft --manual --sim-slot <your slot> --playoff-weight 0.3 --sos-weight 0.5
```

Opens the board next to the Yahoo draft room. As each pick happens: type the
name in the "Mark pick" box (`/` focuses it — "jah gib", "cmc", partial or
misspelled all work), ↑/↓ if needed, **Enter**. The team defaults to whoever
is on the clock; change the picker only for a traded pick. Mis-entry: **↶
Undo** (or Ctrl+Z), or hover the pick in *Recent picks* and click ✕. Every
pick is saved to the store — if the page or the process dies, run the same
command again and it resumes where you were (`--reset-draft` starts over).
The hero always shows "your pick is in N — likely gone by then". The
*League* panel below the board is the whole room: every team's keepers +
picks and what they still need — that's what the survival numbers read.
`PICK_MODEL.md` documents the exact formula behind BEST PICK NOW.

**With Yahoo API access (if ever approved):**

```powershell
.venv\Scripts\python.exe -m fantasy_coach draft --playoff-weight 0.3 --injury-weight 0.5
```

Polls picks every ~2.5s, re-checks Sleeper statuses every 2 min, auto-detects
your team, seeds keepers, and recomputes after every pick. Undos heal
themselves. (`--manual --yahoo-sync` gives you both: hand entry with Yahoo
filling in whatever you haven't marked.)

## Flags

| Flag | What it does |
|---|---|
| `--playoff-weight w` | Draft value = (1−w)·season VORP + w·playoff-week strength (wk15–17). 0 = pure season value. 0.25–0.35 is sane; VORP/tiers never move, only draft value. |
| `--injury-weight w` | How hard documented injury/durability discounts shade draft value. 0 = badges only, ranking unchanged. 0.5 = half the documented discount. |
| `--risk r` | Floor↔ceiling tilt in [-1,1]. 0 = median. `-0.5` leans on floors (safe team), `+0.5` on ceilings (upside — chasing a title). VORP/tiers never move. |
| `--sos-weight s` | Per-week SOS mix in [0,1]: every week valued through its own position-specific matchup; playoff weeks weighted heavier via `--playoff-weight` on top. 0.3–0.5 is sane. |
| `--sim-seed N` | [simulate] A different, reproducible bot room. |
| `--manual` | Live draft by hand entry (no Yahoo). `--sim-slot N` = your slot; `--reset-draft` forgets stored picks; `--yahoo-sync` best-effort overlay. |
| `--status-interval s` | Seconds between live Sleeper status re-checks (default 120; don't go below ~60 — the blob is big). |
| `--team <key>` | Your team if auto-detect can't find it. |
| `--no-warm` | Skip the startup re-warm (use if network dies — runs fully from the store). |
| `--port` / `--no-browser` | Board page options. |

All four dials default to `PLAYOFF_EMPHASIS` / `INJURY_EMPHASIS` /
`RISK_PREFERENCE` / `SOS_EMPHASIS` in `.env` (0.0 = off; the board is then
bit-identical to the certified base). `--sim-slot` defaults to
`draft.my_slot` in the league spec; keepers in the spec are scripted into the
simulated draft (kept players gone from pick 1, each in its cost round) and
the recommendation carries a keeper-eligibility line every pick. Survival probabilities need ADP (Yahoo
`draft_analysis`, pulled by `refresh`/warm) — without it they fall back to
board rank with a wide spread and say so (`source: rank`).

Optional: `PROJECTION_SOURCE=consensus` in `.env` blends the nflverse model
with market-implied (ADP-calibrated) points before the board is built — set it
**before** running `refresh` so the consensus cache warms; leave it unset for
the certified single-source behavior (README "Consensus projections").

## What can go wrong

1. **"No stored settings" / empty board** — you ran from the wrong directory,
   so it opened a fresh empty `data/coach.sqlite3`. `cd` to the project root
   (or set `FANTASY_COACH_DB_PATH`).
2. **Token expired/revoked at draft time** — `status` shows EXPIRED and refresh
   fails: run `login` again (60 seconds), then relaunch `draft`.
3. **Picks show as "Unmapped pick"** — the store was never crosswalked with
   Yahoo ids. Ctrl+C, run the authed `refresh --league <key>` (step 1), then
   `draft` again. A handful of unmapped picks (obscure players) is fine — they
   still leave the pool count.
4. **Network/nflverse dies mid-warm** — every source degrades to its cache
   with a visible WARNING and the board still builds; worst case add
   `--no-warm`. The draft loop itself needs only Yahoo.
5. **Recommendation greys out ("stale")** — polling stalled (Yahoo hiccup).
   It recovers by itself; trust it again once the age badge resets.
