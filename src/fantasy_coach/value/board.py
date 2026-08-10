"""The value engine: VORP, roster-derived baselines, tiers, and the draft board.

M4 §4.2, end to end:

1. **Rescore** every projection's component stat line through the league's own
   scoring (:func:`~fantasy_coach.value.scoring.league_scoring` — §4.1 step 1).
2. **Replacement baselines from the league's real roster demand.** Replacement
   level at a position is the best player who would *not* be started league-wide:
   dedicated slots consume ``num_teams × count`` players per position, then each
   FLEX slot instance (``W/R/T``, superflex ``Q/W/R/T``, …) greedily consumes the
   highest-scoring remaining player among its eligible positions. The baseline is
   the next player after all that consumption — so 2RB vs 3RB leagues, extra
   flexes, and superflex all *move the baseline*, which is the framework's
   "dynamic baselines" requirement (§4.2). No constant cutoffs anywhere.
3. **VORP** = league-scored points − baseline(position). This is what makes the
   cross-position "RB or WR here?" comparison valid.
4. **Tiers** cluster players within a position by VORP gaps — a tier break is a
   cliff (§4.2 "tier cliffs"), the draft-pressure signal M5 alerts on.
5. **Gap-filling** (the step-1 flagged holes): players with no nflverse history
   (rookies) or no stat line at all (K, DEF) do not silently vanish. Anyone with
   a market ADP gets an **ADP-derived value** — a log-linear ``VORP ≈ a + b·ln(adp)``
   curve fit on the players we *can* project — and K/DEF with no ADP get a
   late-round flat value. Every entry is labelled with its ``value_source``
   (``"projection"`` / ``"adp"`` / ``"flat"``) so the honesty posture survives
   into the board.
6. **Schedule awareness** (step 5, optional): given a
   :class:`~fantasy_coach.ingest.schedule.SeasonSchedule`, each projection-based
   entry also gets a matchup-adjusted **playoff VORP** over the league's own
   fantasy-playoff weeks and a **blended draft value**
   ``(1−w)·VORP + w·annualized playoff VORP`` (see
   :mod:`fantasy_coach.value.schedule`). The board ranks by draft value —
   which **equals VORP exactly** when the schedule is absent or
   ``playoff_weight=0``, so the schedule layer is off-by-default-safe. Byes
   are filled from the schedule when the canonical layer lacks them.
7. **Injury awareness** (step 6, optional): given a per-player risk index
   (:func:`~fantasy_coach.value.injury.build_risk_index` — merged current
   status + durability history), every entry is stamped with its status code,
   durability flag, and a short honest note. With ``injury_weight > 0`` the
   combined, clamped discount (:mod:`fantasy_coach.value.injury`) shades the
   entry's **draft value** — never its raw VORP or its tier, which stay pure
   talent measures. ``injury_weight=0`` keeps ordering bit-identical to the
   pre-step-6 board (flags visible, ranking untouched) — off-by-default-safe,
   same contract as the schedule layer.

Everything is parameterized by the passed-in :class:`LeagueSettings` — different
leagues produce different boards from the same projections. Joins run on the
canonical/gsis id via :class:`~fantasy_coach.ingest.canonical.CanonicalPlayer`
(never on name — §3.2).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from fantasy_coach.clients.models import BENCH_POSITIONS, LeagueSettings, RosterPosition
from fantasy_coach.ingest.canonical import CanonicalPlayer
from fantasy_coach.ingest.names import normalize_position
from fantasy_coach.ingest.projections import score_stats
from fantasy_coach.ingest.schedule import SeasonSchedule
from fantasy_coach.ingest.sources import ProjectionRecord
from fantasy_coach.value.injury import PlayerRisk, injury_note, total_discount
from fantasy_coach.value.schedule import (
    blend_value,
    playoff_weeks,
    schedule_note,
    weekly_points,
)
from fantasy_coach.value.scoring import league_scoring

__all__ = [
    "SOURCE_PROJECTION",
    "SOURCE_ADP",
    "SOURCE_FLAT",
    "BoardEntry",
    "ValueBoard",
    "starter_demand",
    "replacement_baselines",
    "assign_tiers",
    "build_value_board",
]

#: ``BoardEntry.value_source`` labels — how each player's value was derived.
SOURCE_PROJECTION = "projection"  # league-rescored stat-line projection (VORP)
SOURCE_ADP = "adp"  # ADP-derived pseudo-VORP (no projection — rookies, K/DEF)
SOURCE_FLAT = "flat"  # no projection *and* no ADP (deep K/DEF) — late-round flat

#: Yahoo flex-slot letter → position code (``W/R/T`` → WR/RB/TE, ``Q/W/R/T``
#: superflex adds QB). Full codes pass through so ``QB/WR`` style slots work too.
_FLEX_CODES = {"Q": "QB", "W": "WR", "R": "RB", "T": "TE", "D": "DEF"}

#: When a league has no usable team count (settings built offline), assume the
#: most common size. Only a fallback — ``build_value_board(num_teams=…)`` and
#: ``settings.max_teams`` both take precedence.
_DEFAULT_NUM_TEAMS = 12


@dataclass(slots=True)
class BoardEntry:
    """One player on the ranked value board.

    ``points`` is the league-rescored season projection (``None`` for ADP/flat
    entries — there is no stat line to score). ``vorp`` is comparable across
    positions and across value sources: for ``"adp"``/``"flat"`` entries it is a
    market-implied estimate placed on the same scale, and ``value_source`` says
    exactly which kind of number you are looking at.

    The schedule-aware fields (step 5) are ``None``/empty when the board was
    built without a schedule: ``playoff_vorp`` is the matchup-adjusted value
    over the league's playoff weeks, ``draft_value`` the season↔playoff blend
    the board ranks by (``None`` means "rank by ``vorp``" — the two are equal
    whenever the playoff emphasis is off), and ``schedule_note`` a short
    narration when the playoff schedule is notably soft/tough.

    The injury fields (step 6) are stamped whenever a risk index was given:
    ``injury_status`` the normalized current designation (``Q``/``O``/``IR``…,
    empty = healthy), ``injury_detail`` the body part when known,
    ``durability_risk`` the categorical games-missed flag,
    ``injury_discount`` the fraction actually shaved off the draft value
    (``None`` when nothing was shaded — including the whole board at
    ``injury_weight=0``), and ``injury_note`` the honest one-line narration.
    """

    canonical_id: str
    name: str
    position: str
    team: str = ""
    points: float | None = None
    vorp: float = 0.0
    value_source: str = SOURCE_PROJECTION
    overall_rank: int = 0
    pos_rank: int = 0
    tier: int = 0
    adp: float | None = None
    bye_week: int | None = None
    playoff_points: float | None = None
    playoff_vorp: float | None = None
    draft_value: float | None = None
    schedule_note: str = ""
    injury_status: str = ""
    injury_detail: str = ""
    durability_risk: str = ""
    injury_discount: float | None = None
    injury_note: str = ""

    @property
    def is_projection_based(self) -> bool:
        """True when ``vorp`` comes from a rescored stat-line projection."""
        return self.value_source == SOURCE_PROJECTION

    @property
    def rank_value(self) -> float:
        """What this entry ranks by: the blended draft value, else raw VORP."""
        return self.vorp if self.draft_value is None else self.draft_value


@dataclass(slots=True)
class ValueBoard:
    """The ranked, cross-position value board for one league's exact rules.

    Attributes:
        entries: All entries, sorted by VORP descending (``overall_rank`` order).
        baselines: ``{position: replacement points}`` derived from roster demand.
        scoring: The ``{stat_key: pts}`` map the board was scored under.
        num_teams: League size the baselines assumed.
        skipped_no_signal: Players dropped for having neither a projection nor
            an ADP (undraftable — but the count is surfaced, not hidden).
        playoff_weeks: The fantasy-playoff weeks the schedule fields used
            (empty when the board was built without a schedule).
        playoff_weight: The blend weight ``w`` the draft values were built
            with (0.0 = pure season VORP — the pre-step-5 board).
        injury_weight: The injury/durability discount weight the draft values
            were built with (0.0 = risk flags only, no value shading — the
            pre-step-6 ranking).
    """

    entries: list[BoardEntry] = field(default_factory=list)
    baselines: dict[str, float] = field(default_factory=dict)
    scoring: dict[str, float] = field(default_factory=dict)
    num_teams: int = _DEFAULT_NUM_TEAMS
    skipped_no_signal: int = 0
    playoff_weeks: list[int] = field(default_factory=list)
    playoff_weight: float = 0.0
    injury_weight: float = 0.0

    def top(self, n: int = 30) -> list[BoardEntry]:
        """The top ``n`` overall — the draft-board view."""
        return self.entries[:n]

    def at_position(self, position: str) -> list[BoardEntry]:
        """Entries at one position, in positional-rank order."""
        pos = normalize_position(position)
        return sorted(
            (e for e in self.entries if e.position == pos), key=lambda e: e.pos_rank
        )


# --------------------------------------------------------------------------- #
# Replacement baselines from roster demand (§4.2 "dynamic baselines")
# --------------------------------------------------------------------------- #


def _slot_positions(slot: RosterPosition) -> list[str]:
    """The normalized position(s) a starting slot can hold (flex → several)."""
    return [
        normalize_position(_FLEX_CODES.get(code, code))
        for code in slot.flex_positions
    ]


def starter_demand(
    settings: LeagueSettings, num_teams: int
) -> tuple[dict[str, int], list[tuple[tuple[str, ...], int]]]:
    """League-wide starter demand from the roster definition.

    Returns ``(dedicated, flex)`` where ``dedicated`` maps a position to the
    total dedicated starting slots across the league (``count × num_teams``) and
    ``flex`` lists each multi-position slot as ``(eligible_positions, total)``.
    Bench/IR slots never count, whatever their ``is_starting_position`` flag says.
    """
    dedicated: dict[str, int] = {}
    flex: list[tuple[tuple[str, ...], int]] = []
    for slot in settings.roster_positions:
        if not slot.is_starting_position or slot.position in BENCH_POSITIONS:
            continue
        if slot.count <= 0:
            continue
        if slot.is_flex:
            flex.append((tuple(_slot_positions(slot)), slot.count * num_teams))
        else:
            pos = normalize_position(slot.position)
            dedicated[pos] = dedicated.get(pos, 0) + slot.count * num_teams
    return dedicated, flex


def replacement_baselines(
    points_by_pos: dict[str, Sequence[float]],
    settings: LeagueSettings,
    num_teams: int,
) -> dict[str, float]:
    """Replacement-level points per position, from the league's real demand.

    ``points_by_pos`` holds each position's league-scored projections sorted
    descending. Dedicated slots consume the top of each position's pool; then
    every flex slot instance greedily takes the best remaining player among its
    eligible positions (a superflex therefore drains QBs exactly as far as QB
    scoring advantage warrants). The baseline is the first player left standing
    — the best player nobody would start.

    A pool exhausted by demand (fewer projected players than starters) baselines
    at its last player's points; an empty pool gets no baseline entry.
    """
    pools = {pos: list(pts) for pos, pts in points_by_pos.items() if pts}
    consumed = {pos: 0 for pos in pools}
    dedicated, flex = starter_demand(settings, num_teams)

    for pos, demand in dedicated.items():
        if pos in pools:
            consumed[pos] = min(demand, len(pools[pos]))

    for eligible, total in flex:
        for _ in range(total):
            best_pos: str | None = None
            best_pts = -math.inf
            for pos in eligible:
                pool = pools.get(pos)
                if pool is None or consumed[pos] >= len(pool):
                    continue
                pts = pool[consumed[pos]]
                if pts > best_pts:
                    best_pos, best_pts = pos, pts
            if best_pos is None:
                break  # every eligible pool exhausted
            consumed[best_pos] += 1

    return {
        pos: pool[consumed[pos]] if consumed[pos] < len(pool) else pool[-1]
        for pos, pool in pools.items()
    }


# --------------------------------------------------------------------------- #
# Tiers (§4.2 "tiers & tier cliffs")
# --------------------------------------------------------------------------- #


def assign_tiers(
    values: Sequence[float],
    *,
    gap_factor: float = 1.5,
    min_gap: float = 10.0,
    fixed_gap: float | None = None,
) -> list[int]:
    """Cluster a descending value list into tiers by gap size.

    A new tier starts wherever the drop to the next player exceeds the
    threshold: ``fixed_gap`` when given (tests / manual control), otherwise
    ``max(min_gap, gap_factor × mean gap)`` — adaptive to the position's spread
    so a flat position doesn't shatter into single-player tiers and a steep one
    doesn't collapse into a single tier. A big gap *is* the tier cliff (§4.2).

    The ``min_gap`` default is sized for **season-long** points (a ~10-point
    season gap ≈ 0.6 pts/week — anything smaller is projection noise, not a
    cliff); weekly boards should pass a smaller floor.
    """
    if not values:
        return []
    gaps = [values[i - 1] - values[i] for i in range(1, len(values))]
    if fixed_gap is not None:
        threshold = fixed_gap
    else:
        mean_gap = sum(gaps) / len(gaps) if gaps else 0.0
        threshold = max(min_gap, gap_factor * mean_gap)
    tiers = [1]
    for gap in gaps:
        tiers.append(tiers[-1] + 1 if gap > threshold else tiers[-1])
    return tiers


# --------------------------------------------------------------------------- #
# ADP → value curve (the rookie / K / DEF gap-fill)
# --------------------------------------------------------------------------- #


def _fit_adp_curve(pairs: Sequence[tuple[float, float]]):
    """Fit ``vorp ≈ a + b·ln(adp)`` on (adp, vorp) pairs of *projected* players.

    Least squares on ``x = ln(adp)`` — the market's value decay is roughly
    log-shaped (pick 1→13 matters far more than 101→113). Returns a callable
    ``adp → estimated vorp``. Degenerate inputs (fewer than two distinct ADPs)
    fall back to the mean VORP of the pairs, or 0.0 with no pairs at all — the
    honest "we know nothing" flat estimate.
    """
    pts = [(math.log(max(adp, 1.0)), vorp) for adp, vorp in pairs if adp is not None]
    if len({x for x, _ in pts}) < 2:
        flat = sum(v for _, v in pts) / len(pts) if pts else 0.0
        return lambda adp: flat
    n = len(pts)
    mean_x = sum(x for x, _ in pts) / n
    mean_y = sum(y for _, y in pts) / n
    ss_xx = sum((x - mean_x) ** 2 for x, _ in pts)
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in pts)
    b = ss_xy / ss_xx
    a = mean_y - b * mean_x
    return lambda adp: a + b * math.log(max(adp, 1.0))


# --------------------------------------------------------------------------- #
# The board
# --------------------------------------------------------------------------- #


def build_value_board(
    projections: Sequence[ProjectionRecord],
    settings: LeagueSettings,
    *,
    num_teams: int | None = None,
    players: Iterable[CanonicalPlayer] | None = None,
    schedule: SeasonSchedule | None = None,
    playoff_weight: float = 0.0,
    risk: Mapping[str, PlayerRisk] | None = None,
    injury_weight: float = 0.0,
    tier_gap_factor: float = 1.5,
    tier_min_gap: float = 10.0,
    tier_fixed_gap: float | None = None,
) -> ValueBoard:
    """Turn projections + this league's exact rules into a ranked value board.

    Args:
        projections: Stat-line projections keyed by gsis id (``source_id_field
            == "gsis_id"`` — the hub, so the join is the happy path). Records
            without a component ``stats`` line fall back to their pre-scored
            ``points`` (still labelled projection-based).
        settings: The league's real settings — scoring *and* roster demand both
            come from here; nothing is hardcoded. The fantasy-playoff weeks for
            the schedule fields come from here too (§step 5).
        num_teams: League size. Defaults to ``settings.max_teams``, then 12.
        players: Optional canonical players (from M3's ``PlayerIndex``). Enables
            the gap-fill paths: anyone here *without* a matching projection gets
            ADP-derived value (``market.adp``) or, for K/DEF, a late-round flat
            entry. Matched players also contribute their ADP/bye to the entry,
            and the computed value is written back onto ``player.value`` so the
            canonical layer carries the result (M3 §3.3 "attached" slots).
        schedule: Optional season schedule + opponent difficulty (step 5).
            Enables playoff VORP, blended draft values, schedule notes, and
            bye-week fill-in. ``None`` → the board is exactly the season board.
        playoff_weight: The blend weight ``w`` in ``(1−w)·VORP + w·annualized
            playoff VORP``. ``0.0`` (default) preserves pre-step-5 ranking even
            with a schedule present; ~``0.2–0.35`` is a meaningful playoff tilt.
        risk: Optional per-player injury picture (step 6) —
            ``{canonical_id: PlayerRisk}`` from
            :func:`~fantasy_coach.value.injury.build_risk_index`. Enables the
            status/durability stamps + notes on every matched entry.
        injury_weight: How hard the combined injury discount shades draft
            values. ``0.0`` (default) stamps flags but changes **no** value or
            rank — the off-by-default-safe contract; ~``0.5–1.0`` applies a
            half-to-full share of the documented discounts.

    Returns:
        A :class:`ValueBoard` sorted by draft value (== VORP when the playoff
        emphasis is off) with per-position ranks and VORP-based tiers.
    """
    scoring = league_scoring(settings)
    teams = num_teams or settings.max_teams or _DEFAULT_NUM_TEAMS

    by_gsis: dict[str, CanonicalPlayer] = {}
    if players is not None:
        players = list(players)
        for p in players:
            if p.ids.gsis_id:
                by_gsis[p.ids.gsis_id] = p

    # 1. Rescore every projection into this league's points.
    entries: list[BoardEntry] = []
    projected_gsis: set[str] = set()
    for rec in projections:
        canonical = by_gsis.get(rec.source_id)
        position = normalize_position(canonical.position if canonical else rec.position)
        if not position:
            continue
        projected_gsis.add(rec.source_id)
        points = score_stats(rec.stats, scoring) if rec.stats else rec.points
        entries.append(
            BoardEntry(
                canonical_id=canonical.canonical_id if canonical else rec.source_id,
                name=(canonical.name if canonical else "") or rec.name,
                position=position,
                team=(canonical.team if canonical else "") or rec.team,
                points=round(points, 2),
                value_source=SOURCE_PROJECTION,
                adp=canonical.market.adp if canonical else None,
                bye_week=canonical.bye_week if canonical else None,
            )
        )

    # 2. Baselines from roster demand, 3. VORP.
    points_by_pos: dict[str, list[float]] = {}
    for e in entries:
        points_by_pos.setdefault(e.position, []).append(e.points or 0.0)
    for pool in points_by_pos.values():
        pool.sort(reverse=True)
    baselines = replacement_baselines(points_by_pos, settings, teams)
    for e in entries:
        e.vorp = round((e.points or 0.0) - baselines.get(e.position, 0.0), 2)

    # 5. Gap-fill from ADP / flat for players the projection model can't see.
    skipped = 0
    if players is not None:
        curve = _fit_adp_curve(
            [(e.adp, e.vorp) for e in entries if e.adp is not None]
        )
        for p in players:
            if (p.ids.gsis_id or "") in projected_gsis:
                continue
            if not p.position:
                continue
            adp = p.market.adp
            if adp is not None:
                entries.append(
                    BoardEntry(
                        canonical_id=p.canonical_id,
                        name=p.name,
                        position=p.position,
                        team=p.team,
                        points=None,
                        vorp=round(curve(adp), 2),
                        value_source=SOURCE_ADP,
                        adp=adp,
                        bye_week=p.bye_week,
                    )
                )
            elif p.position in ("K", "DEF"):
                entries.append(
                    BoardEntry(
                        canonical_id=p.canonical_id,
                        name=p.name,
                        position=p.position,
                        team=p.team,
                        points=None,
                        vorp=0.0,
                        value_source=SOURCE_FLAT,
                        bye_week=p.bye_week,
                    )
                )
            else:
                skipped += 1  # no projection, no ADP, not K/DEF — undraftable

    # 6. Schedule awareness (step 5): playoff VORP + blended draft value.
    pweeks = playoff_weeks(settings) if schedule is not None else []
    if schedule is not None and pweeks:
        season_weeks = max(pweeks)  # the fantasy season runs through the final
        for e in entries:
            if e.bye_week is None and e.team:
                e.bye_week = schedule.bye_week(e.team)
            if not e.is_projection_based or e.points is None or not e.team:
                continue  # ADP/flat entries have no stat line to split
            weekly = weekly_points(
                e.points, e.team, e.position, schedule, through_week=season_weeks
            )
            if not weekly:
                continue  # unknown team — no weekly view, stay season-only
            p_points = sum(weekly.get(w, 0.0) for w in pweeks)
            baseline = baselines.get(e.position, 0.0)
            p_baseline = baseline * len(pweeks) / len(weekly)
            e.playoff_points = round(p_points, 2)
            e.playoff_vorp = round(p_points - p_baseline, 2)
            e.draft_value = round(
                blend_value(
                    e.vorp,
                    e.playoff_vorp,
                    weight=playoff_weight,
                    n_playoff_weeks=len(pweeks),
                    n_season_weeks=len(weekly),
                ),
                2,
            )
            e.schedule_note = schedule_note(e.position, e.team, schedule, pweeks)

    # 7. Injury awareness (step 6): stamp flags always; shade draft value only
    # when the injury weight is on. Raw VORP and tiers are never touched — a
    # hurt player is the same talent, just a riskier bet.
    if risk is not None:
        applied_pw = playoff_weight if pweeks else 0.0
        for e in entries:
            r = risk.get(e.canonical_id)
            if r is None:
                continue
            e.injury_status = r.status
            e.injury_detail = r.report.detail if r.report is not None else ""
            e.durability_risk = r.durability_risk
            shaded_pct = None
            if injury_weight > 0:
                discount = injury_weight * total_discount(
                    r, playoff_weight=applied_pw
                )
                if discount > 0 and e.rank_value > 0:
                    e.injury_discount = round(discount, 3)
                    e.draft_value = round(e.rank_value * (1.0 - discount), 2)
                    shaded_pct = discount * 100.0
            e.injury_note = injury_note(r, shaded_pct=shaded_pct)

    # Ranks: overall by draft value — identical to VORP order when the playoff
    # emphasis is off (ties → VORP, points, then name for determinism).
    entries.sort(key=lambda e: (-e.rank_value, -e.vorp, -(e.points or 0.0), e.name))
    for rank, e in enumerate(entries, start=1):
        e.overall_rank = rank

    # 4. Positional ranks + gap-based tiers (ADP/flat entries participate so the
    # positional view stays complete; their VORP is market-implied by design).
    # Tiers cluster *season* VORP — a tier cliff is a talent gap, and it must
    # not move when the playoff dial does — so the position list is re-sorted
    # by VORP even when the board order is blended.
    for pos in {e.position for e in entries}:
        pos_entries = sorted(
            (e for e in entries if e.position == pos),
            key=lambda e: (-e.vorp, -(e.points or 0.0), e.name),
        )
        tiers = assign_tiers(
            [e.vorp for e in pos_entries],
            gap_factor=tier_gap_factor,
            min_gap=tier_min_gap,
            fixed_gap=tier_fixed_gap,
        )
        for i, (e, tier) in enumerate(zip(pos_entries, tiers), start=1):
            e.pos_rank = i
            e.tier = tier

    # Write value back onto the canonical layer (M4 fills the §3.3 slots).
    if players is not None:
        by_canonical = {e.canonical_id: e for e in entries}
        for p in players:
            e = by_canonical.get(p.canonical_id)
            if e is not None:
                p.value.vorp = e.vorp
                p.value.tier = e.tier
                p.value.scoring_adjusted_points = e.points

    return ValueBoard(
        entries=entries,
        baselines={pos: round(b, 2) for pos, b in baselines.items()},
        scoring=scoring,
        num_teams=teams,
        skipped_no_signal=skipped,
        playoff_weeks=pweeks,
        playoff_weight=playoff_weight if schedule is not None else 0.0,
        injury_weight=injury_weight if risk is not None else 0.0,
    )
