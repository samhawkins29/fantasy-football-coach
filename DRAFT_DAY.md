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

## 2. The week before: dress rehearsal

```powershell
.venv\Scripts\python.exe -m fantasy_coach vintage      # every slice should be recent
.venv\Scripts\python.exe -m fantasy_coach draft --simulate --sim-slot <your slot> --playoff-weight 0.3 --injury-weight 0.5
```

Offline replay of a full snake draft through the identical live loop at
http://localhost:8787. Practice reading the board: BEST PICK NOW, tier cliffs,
injury badges, the vintage footer.

## 3. Draft day (morning)

```powershell
.venv\Scripts\python.exe -m fantasy_coach status       # token still good? if not: login
.venv\Scripts\python.exe -m fantasy_coach refresh      # latest injuries/ADP; check the After table
```

## 4. Draft day (when the room opens)

```powershell
.venv\Scripts\python.exe -m fantasy_coach draft --playoff-weight 0.3 --injury-weight 0.5
```

Opens the board next to the Yahoo draft room. It polls picks every ~2.5s,
re-checks Sleeper statuses every 2 min, auto-detects your team, seeds keepers,
and recomputes the recommendation after every pick. Undos heal themselves.

## Flags

| Flag | What it does |
|---|---|
| `--playoff-weight w` | Draft value = (1−w)·season VORP + w·playoff-week strength (wk15–17). 0 = pure season value. 0.25–0.35 is sane; VORP/tiers never move, only draft value. |
| `--injury-weight w` | How hard documented injury/durability discounts shade draft value. 0 = badges only, ranking unchanged. 0.5 = half the documented discount. |
| `--status-interval s` | Seconds between live Sleeper status re-checks (default 120; don't go below ~60 — the blob is big). |
| `--team <key>` | Your team if auto-detect can't find it. |
| `--no-warm` | Skip the startup re-warm (use if network dies — runs fully from the store). |
| `--port` / `--no-browser` | Board page options. |

Both weights default to `PLAYOFF_EMPHASIS` / `INJURY_EMPHASIS` in `.env` (0.0).

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
