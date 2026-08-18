# How the Coach picks a player — the full formula

This is the audit target: every number that goes into "BEST PICK NOW", in the
order the code computes it, with the module that owns each step. Every dial
defaults to **0 = off**, and with all dials off the pick is *need-weighted
season VORP* exactly as certified in M4/M5. Nothing below is hidden; each
step is a labelled model estimate.

Notation: one league (`LeagueSettings`), `T` teams, the **available pool** =
every player not kept and not yet drafted (recomputed on every poll).

## 0. Inputs (free, offline)

| Input | Source | Module |
|---|---|---|
| Season stat-line projections (QB/RB/WR/TE + IDP DL/LB/DB) | nflverse weekly history, recency-weighted per-game rates regressed to positional priors | `ingest/projections.py` |
| Floor / ceiling (~20th/80th pct) | week-to-week scoring variance shrunk to positional priors + role/sample uncertainty; consensus widens for source disagreement | `ingest/variance.py`, `ingest/consensus.py` |
| Schedule + per-position opponent multipliers | nflverse schedules + prior-season points allowed per position, clamped 0.75–1.25 | `ingest/schedule.py` |
| Injury status + durability | Sleeper/Yahoo status, nflverse games-missed history | `ingest/injury.py`, `value/injury.py` |
| ADP (survival) | Yahoo `draft_analysis` when available; else board rank fallback | `draft/survival.py` |
| League rules, keepers, draft picks | `data/league.json` → store (`league_settings`, `keepers`, `draft_picks`) | `league.py`, `store/` |

## 1. League points  (`value/scoring.py`, `value/board.py` step 1)

`points = Σ_stat  projected_stat × league_points_per_unit` — the raw stat
line is rescored through the league's own modifiers (full PPR `rec = 1.0`,
4-pt pass TD, IDP tackle 1 / sack 2 / INT 3 …). Reference half-PPR points are
never used for value.

## 2. Replacement level and VORP  (`value/board.py` steps 2–3)

For each position, players in the **available pool** are sorted by league
points. Starter demand comes from the roster: dedicated slots consume
`count × T` players per position (`QB 1·10, RB 2·10, WR 2·10, DEF 1·10`),
then every flex instance (`W/R/T` ×2·10, IDP `D` ×1·10 over DL/LB/DB) greedily
consumes the best remaining player among its eligible positions. The
**baseline** is the first player left standing at each position; positions no
slot accepts (K in this league) are dropped from the board.

`VORP = points − baseline[position]`

Because the pool is the *available* pool, keepers and made picks move the
baselines every poll (fewer RBs left → RB replacement slides down).
Tiers cluster season VORP by gap size; a tier's last available player is a
**cliff**.

## 3. Distribution  (`value/board.py` step 3b)

`floor = points × floor_ratio`, `ceiling = points × ceiling_ratio` (ratios from
the projection source, scoring-invariant); `floor_vorp = floor − baseline`,
`ceiling_vorp = ceiling − baseline`.

## 4. Schedule: per-week SOS and playoff weeks  (`value/schedule.py`, board step 6)

With a cached schedule, `points` are split evenly over the team's game weeks
1…17 (week 18 has no value; bye week absent) and each week is scaled by the
opponent's position-specific multiplier:

* `sos_vorp = Σ_w weekly_w − baseline` (every week through its own matchup)
* `playoff_vorp = Σ_{w∈{15,16,17}} weekly_w − baseline × 3/16`
* `season_component = (1 − s)·VORP + s·sos_vorp`   — dial **s = SOS_EMPHASIS**
* `draft_value = (1 − w)·season_component + w·(playoff_vorp × 16/3)` — dial **w = PLAYOFF_EMPHASIS** (annualized so both terms share a scale; the playoff weeks are therefore weighted more heavily on top of the per-week adjustment)

`sos_score` (display) = mean weekly multiplier with playoff weeks counted 2×.

## 5. Risk preference  (board step 8, dial **r = RISK_PREFERENCE ∈ [−1, 1]**)

`draft_value += r·(ceiling_vorp − VORP)` if `r > 0`, else `draft_value −= |r|·(VORP − floor_vorp)`.
`r = 0` leaves the median. Only draft value moves — VORP and tiers never do.

## 6. Injury / durability  (board step 7, dial **INJURY_EMPHASIS**)

`draft_value ×= (1 − injury_weight × discount)` where `discount` is the
documented, clamped combination of current status (O/IR/Q…) and durability
history. Flags always show; shading only with the dial on.

`rank_value = draft_value` (or `VORP` when no schedule/dials) — the board's
overall order.

## 7. Roster need and bye stacking  (`draft/recommend.py`, `rank_available`)

For **your** roster (keepers + picks assigned to slots):

`score = rank_value × need_weight` for positive values, where
`need_weight = 1.00` (an open dedicated starter at the position) / `0.85`
(only an open flex fits it) / `0.55` (starters set — depth). Negative values
are never inflated. A player whose bye is already shared by more than one of
your starters loses `4 %` per extra collision (max 12 %).

Players are ranked by `score` (ties → VORP). This ranking is the "Top
available" table.

## 8. Survival — will he be there next time?  (`draft/survival.py`)

For every available player: draft slot `T ~ N(ADP_eff, σ)`,
`σ = max(2.5, 0.12·ADP)` (source stdev if present; no ADP → rank on the
available board as pseudo-ADP, `σ = 18`).

`ADP_eff = ADP + drift − run_shift` where `drift` = median(pick − ADP) over the
last 24 picks (±6) and `run_shift = min(σ, 8) × run_excess[position]`
(observed share of the position in the last 8 picks vs the market's expected
share, capped 1).

`P(available at pick n | available now at c) = S(n′ − ½) / S(c − ½)`, `S` the
normal survival function, with the *effective* pick distance
`n′ = c + (n − c) × need_scale[position]` — `need_scale` = mean need weight
(step 7's 1.0/0.85/0.55) of the **teams actually picking between now and n**
÷ the room's mean, clamped 0.25–2. Teams whose starters at a position are
kept/filled make that position safer. Pre-made keeper picks are not live
selections and are excluded from the count.

Outputs `p_next` (your next pick), `p_after` (the one after) and a label
(take now < .35 < coin flip < .65 < likely there < .85 < safe to wait).

## 9. The recommendation  (`build_recommendation`, `lookahead_pick`)

`best = ranked[0]` unless the **two-pick lookahead** flips it: for candidate
`B` among the next three with `score_B ≥ 0.8·score_A`, `p_B < 0.65` and
`p_A ≥ 0.85`, compare
`EV(A now) = s_A + p_B·s_B + (1−p_B)·s_F` with
`EV(B now) = s_B + p_A·s_A + (1−p_A)·s_F` (`s_F` = the score of the ranked
player you'd otherwise get at your next pick, index ≈ picks until then). The
larger EV wins; ties → the value pick. Ranking never changes — survival only
decides *timing* between near-equals.

The reasons list narrates each factor: value (VORP / blended draft value),
need, cliff, floor–ceiling range, schedule note (playoff weeks, extreme week),
injury, bye stacking, ADP fall, survival ("won't last" / "plan: X should still
be there"), positional run, and keeper eligibility (`round < 4` → not
keepable next year; else costs `round − 3`).

## 10. Where keepers enter

* Kept players are pre-made picks in their cost round from pick 1 → out of
  the pool (steps 2+), on their team's roster (steps 7–8), and their team makes
  no live selection in that round (the clock and "your next pick" skip it).
* Cost round: `last_round − 3` (rounds 1–3 un-keepable), undrafted → 15 then
  14/16/17 (`league.assign_keeper_rounds`).

## 11. What the mock opponents (simulation) do  (`draft/bots.py`)

Utility in picks: `(1−v)·(−ADP) + v·(−board rank) + need (6 starter / 3 flex)
+ run (4 × sensitivity, only if the bot needs the position) + handcuff 10 +
bye −3/extra + archetype bias + elite QB/TE premium 8 − backup penalty 4 +
Gaussian noise (reach × 0.12 × pick)`, under hard caps (QB 2, TE 2, K/DEF 1
and last 2 rounds, IDP ≤ 2 in the back stretch, must-fill starters). They
consume the same keeper triples and roster machinery as the live loop.

## Dials (all default 0 = certified base)

`PLAYOFF_EMPHASIS` (w) · `SOS_EMPHASIS` (s) · `RISK_PREFERENCE` (r) ·
`INJURY_EMPHASIS` — env or `draft --playoff-weight/--sos-weight/--risk/--injury-weight`.
Suggested for this league: `w 0.3, s 0.3–0.5, r 0…+0.3`.
