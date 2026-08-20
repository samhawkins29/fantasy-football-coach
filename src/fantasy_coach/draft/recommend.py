"""Roster-need weighting and the BEST-PICK-NOW recommendation (M5, §4.2–4.3).

The draft rule this encodes: *best available, bent toward what you still have
to start.* Raw VORP already makes players comparable across positions (M4);
this layer multiplies it by a **need weight** derived from the founder's actual
unfilled lineup slots under the league's real roster definition:

* an open **dedicated** starting slot at the position → full weight (1.0);
* only a **flex** slot left that the position fits → slightly damped (0.85) —
  a flex can be filled from several positions, so no single one is urgent;
* all starting demand met → **depth** weight (0.55) — still on the board
  (value is value, and bench wins leagues), but a starter-filling pick of
  comparable value should beat it.

Negative-VORP players are *not* inflated by down-weighting (a bad pick at a
filled position is still a bad pick), so the weight only ever scales positive
value down toward zero, never flips orderings among below-replacement players.

Tier cliffs ride along from the M4 board: an available player who is the *last
of their tier* at the position is flagged with the size of the drop to the next
tier — the §4.2 "reach for the cliff" pressure signal — and the recommendation
narrates every factor (value, need, cliff, ADP fall, playoff schedule, bye
stacking, injury status / durability risk) so the founder can trust the pick
at a glance.

Step-5 additions, both nudges by design:

* The value being weighted is the board's **draft value** (the season↔playoff
  blend) when present, raw VORP otherwise — so a playoff emphasis set on the
  board flows through the need weighting unchanged.
* **Bye stacking**: drafting a player whose bye week is already shared by
  several of *your* starters gets a small multiplicative penalty (the first
  shared starter is free — byes collide in any roster; it's the pile-up that
  costs a real week). Capped tight so it reorders near-ties, never overrides a
  clear value gap.

Survival awareness (upgrade 2, framework §4.3): every ranked player carries
their :class:`~fantasy_coach.draft.survival.Survival` estimate, and the
recommendation runs a **two-pick lookahead** over the top candidates. Taking
``A`` now and hoping for ``B`` at your next pick is worth
``s_A + p_B·s_B + (1−p_B)·s_F`` (``s_F`` = what you'd otherwise get there);
the reverse is ``s_B + p_A·s_A + (1−p_A)·s_F``. When ``B`` is close in value,
unlikely to survive, and ``A`` very likely will, the lookahead flips the pick
to ``B`` — "take B now, A should still be there" — and says so. Otherwise the
value pick stands and the survival label ("take now" / "safe to wait") is
simply narrated. Ranking order itself never changes: survival decides *timing*
between near-equals, it doesn't rewrite value.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from fantasy_coach.clients.models import BENCH_POSITIONS, LeagueSettings
from fantasy_coach.draft.survival import (
    COIN_FLIP_BELOW,
    LABEL_TAKE_NOW,
    SAFE_ABOVE,
    TAKE_NOW_BELOW,
    Survival,
)
from fantasy_coach.ingest.names import normalize_position
from fantasy_coach.value.board import ValueBoard, BoardEntry, _FLEX_CODES

__all__ = [
    "NEED_STARTER",
    "NEED_FLEX",
    "NEED_DEPTH",
    "NEED_WEIGHTS",
    "DEPTH_WEIGHTS",
    "STREAM_WAIT_WEIGHT",
    "need_weight",
    "SlotInstance",
    "roster_slots",
    "assign_roster",
    "RosterNeeds",
    "compute_needs",
    "RankedPlayer",
    "Recommendation",
    "rank_available",
    "build_recommendation",
    "lookahead_pick",
    "wait_for",
    "SWAP_MIN_SCORE_RATIO",
]

#: The two-pick lookahead only ever swaps to a candidate worth at least this
#: share of the top score — survival decides timing between near-equals, it
#: never talks you into a clearly worse player.
SWAP_MIN_SCORE_RATIO = 0.80
#: How many of the top-ranked players the lookahead considers swapping to.
SWAP_CANDIDATES = 3

#: Need tags (also the snapshot's per-player ``need`` labels).
NEED_STARTER = "starter"
NEED_FLEX = "flex"
NEED_DEPTH = "depth"

#: The weight each need level applies to a positive VORP.
NEED_WEIGHTS: dict[str, float] = {
    NEED_STARTER: 1.0,
    NEED_FLEX: 0.85,
    NEED_DEPTH: 0.55,
}

#: Position-aware depth weights (once the position's starters are set): bench
#: RB/WR win weeks (injuries + byes make them near-starters), a backup TE is
#: worth little, a backup QB in a 1-QB league less, and a second IDP/DEF/K is
#: a wasted roster spot in a streaming league. Positions not listed use
#: ``NEED_WEIGHTS["depth"]``.
DEPTH_WEIGHTS: dict[str, float] = {
    "RB": 0.6,
    "WR": 0.6,
    "TE": 0.3,
    "QB": 0.2,
    "DL": 0.05,
    "LB": 0.05,
    "DB": 0.05,
    "DEF": 0.05,
    "K": 0.05,
}

#: The need weight a *streamable* open slot (IDP/DEF/K) carries while there is
#: no urgency to fill it — a sharp drafter takes these in the last rounds, so
#: an open DEF slot must not compete with RB/WR value at full starter weight
#: from pick 1. Rises toward the full need weight as urgency → 1 (see
#: ``rank_available``'s ``stream_urgency``).
STREAM_WAIT_WEIGHT = 0.3


def need_weight(tag: str, position: str) -> float:
    """The score multiplier for ``tag`` at ``position`` (depth is position-aware)."""
    if tag == NEED_DEPTH:
        return DEPTH_WEIGHTS.get(position, NEED_WEIGHTS[NEED_DEPTH])
    return NEED_WEIGHTS[tag]

#: Bye-stacking nudge: sharing a bye with this many of your starters is free
#: (one collision is unavoidable roster math)…
BYE_STACK_FREE = 1
#: …each starter beyond that costs this fraction of the positive score…
BYE_PENALTY_STEP = 0.04
#: …capped here so even a five-way pile-up stays a nudge, not a veto.
BYE_PENALTY_MAX = 0.12


# --------------------------------------------------------------------------- #
# Roster slots — one expanded instance per lineup spot, fillable in pick order
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SlotInstance:
    """One concrete lineup spot (``2×RB`` expands to two of these).

    ``eligible`` holds normalized positions the spot accepts; empty means
    anything (bench). ``player`` is filled by :func:`assign_roster` with a
    display dict (name/position/…) or stays ``None`` while the spot is open.
    """

    label: str
    eligible: tuple[str, ...] = ()
    is_flex: bool = False
    is_bench: bool = False
    player: dict[str, object] | None = None

    def accepts(self, position: str) -> bool:
        """Whether ``position`` can fill this spot."""
        return self.is_bench or position in self.eligible


def roster_slots(settings: LeagueSettings) -> list[SlotInstance]:
    """Expand the league's roster definition into per-spot instances.

    Starting slots come first in settings order (dedicated and flex), then
    bench spots; IR/IL spots are omitted — they aren't drafted into.
    """
    slots: list[SlotInstance] = []
    bench: list[SlotInstance] = []
    for slot in settings.roster_positions:
        if slot.count <= 0:
            continue
        if slot.position == "BN":
            bench.extend(
                SlotInstance(label="BN", is_bench=True) for _ in range(slot.count)
            )
            continue
        if slot.position in BENCH_POSITIONS:  # IR/IL — not draftable
            continue
        eligible = tuple(
            normalize_position(_FLEX_CODES.get(code, code))
            for code in slot.flex_positions
        )
        slots.extend(
            SlotInstance(label=slot.position, eligible=eligible, is_flex=slot.is_flex)
            for _ in range(slot.count)
        )
    return slots + bench


def assign_roster(
    slots: list[SlotInstance], players: Iterable[Mapping[str, object]]
) -> list[SlotInstance]:
    """Greedily place drafted players (in pick order) into lineup spots.

    Each player takes their first open dedicated slot, else the first open flex
    they fit, else a bench spot; a full roster overflows onto extra bench rows
    so nothing silently disappears. Players with unknown position (unmapped
    picks) go straight to the bench. Returns ``slots`` (mutated) for chaining.
    """
    for info in players:
        position = normalize_position(str(info.get("position", "") or ""))
        target: SlotInstance | None = None
        if position:
            target = next(
                (s for s in slots if s.player is None and not s.is_bench
                 and not s.is_flex and s.accepts(position)),
                None,
            ) or next(
                (s for s in slots if s.player is None and s.is_flex
                 and s.accepts(position)),
                None,
            )
        if target is None:
            target = next(
                (s for s in slots if s.player is None and s.is_bench), None
            )
        if target is None:
            target = SlotInstance(label="BN", is_bench=True)
            slots.append(target)
        target.player = dict(info)
    return slots


# --------------------------------------------------------------------------- #
# Needs → weights
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RosterNeeds:
    """What the founder still has to fill, derived from assigned slots.

    Attributes:
        open_starters: ``{position: open dedicated starting spots}``.
        open_flex: The eligible-position tuple of each open flex spot.
        open_bench: Open bench spots.
    """

    open_starters: dict[str, int] = field(default_factory=dict)
    open_flex: list[tuple[str, ...]] = field(default_factory=list)
    open_bench: int = 0

    def tag(self, position: str) -> str:
        """The need level drafting ``position`` satisfies right now."""
        if self.open_starters.get(position, 0) > 0:
            return NEED_STARTER
        if any(position in eligible for eligible in self.open_flex):
            return NEED_FLEX
        return NEED_DEPTH

    def weight(self, position: str) -> float:
        """The score multiplier for a positive VORP at ``position``."""
        return need_weight(self.tag(position), position)


def compute_needs(slots: Sequence[SlotInstance]) -> RosterNeeds:
    """Read the open spots out of an assigned slot list."""
    needs = RosterNeeds()
    for slot in slots:
        if slot.player is not None:
            continue
        if slot.is_bench:
            needs.open_bench += 1
        elif slot.is_flex:
            needs.open_flex.append(slot.eligible)
        else:
            pos = slot.eligible[0] if slot.eligible else slot.label
            needs.open_starters[pos] = needs.open_starters.get(pos, 0) + 1
    return needs


# --------------------------------------------------------------------------- #
# Ranking + the recommendation
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RankedPlayer:
    """A board entry re-scored by roster need, with its cliff/bye context."""

    entry: BoardEntry
    score: float
    weight: float
    need: str
    cliff: bool = False
    cliff_drop: float | None = None
    bye_overlap: int = 0
    playoff_weeks: tuple[int, ...] = ()
    survival: Survival | None = None

    def as_dict(self) -> dict[str, object]:
        """The snapshot/JSON shape the web page renders."""
        e = self.entry
        return {
            "canonical_id": e.canonical_id,
            "name": e.name,
            "position": e.position,
            "team": e.team,
            "bye": e.bye_week,
            "points": e.points,
            "vorp": e.vorp,
            "draft_value": e.rank_value,
            "playoff_vorp": e.playoff_vorp,
            "schedule_note": e.schedule_note,
            "injury_status": e.injury_status,
            "injury_detail": e.injury_detail,
            "durability_risk": e.durability_risk,
            "injury_discount": e.injury_discount,
            "injury_note": e.injury_note,
            "floor": e.floor,
            "ceiling": e.ceiling,
            "floor_vorp": e.floor_vorp,
            "ceiling_vorp": e.ceiling_vorp,
            "sos_vorp": e.sos_vorp,
            "sos_score": e.sos_score,
            "playoff_matchups": [
                {"week": w, "mult": m}
                for w, m in sorted(e.week_multipliers.items())
                if w in self.playoff_weeks
            ],
            "adp": e.adp,
            "tier": e.tier,
            "pos_rank": e.pos_rank,
            "overall_rank": e.overall_rank,
            "value_source": e.value_source,
            "score": round(self.score, 2),
            "weight": self.weight,
            "need": self.need,
            "cliff": self.cliff,
            "cliff_drop": self.cliff_drop,
            "bye_overlap": self.bye_overlap,
            "survival": self.survival.as_dict() if self.survival else None,
        }


@dataclass(slots=True)
class Recommendation:
    """The single BEST PICK NOW, with the reasons spelled out.

    ``swapped_from`` is set when the survival lookahead moved the pick off the
    top-scored player (who is expected to still be there next time).
    """

    player: RankedPlayer
    reasons: list[str] = field(default_factory=list)
    swapped_from: RankedPlayer | None = None


def rank_available(
    board: ValueBoard,
    needs: RosterNeeds,
    *,
    starter_bye_counts: Mapping[int, int] | None = None,
    survival: Mapping[str, Survival] | None = None,
    stream_urgency: Mapping[str, float] | None = None,
) -> list[RankedPlayer]:
    """Re-rank the available board by need-weighted score.

    ``board`` must already be the *available-only* board (drafted players
    filtered before the rebuild, so its baselines reflect the drained pool).
    Score = ``weight × draft value`` for positive value (VORP when the board
    carries no schedule blend), raw value otherwise — see the module docstring
    for why negatives are never inflated.

    ``starter_bye_counts`` maps a bye week to how many of *your current
    starters* share it; a positive score is nudged down when drafting the
    player would pile a further starter onto an already-shared bye (module
    docstring: first collision free, then ``BYE_PENALTY_STEP`` per extra
    starter, capped at ``BYE_PENALTY_MAX``).

    ``survival`` (upgrade 2) attaches each player's availability estimate for
    the page and the recommendation's lookahead; it never changes the order.

    ``stream_urgency`` maps a *streamable* position (IDP groups / DEF / K) to
    an urgency in ``[0, 1]``: at 0 an open slot there carries only
    :data:`STREAM_WAIT_WEIGHT` (take it late, like a sharp drafter), rising
    linearly to the full need weight at 1 (endgame, or remaining startable
    supply approaching remaining demand). Positions absent from the map keep
    their normal weight.
    """
    bye_counts = starter_bye_counts or {}
    surv = survival or {}
    urgency_map = stream_urgency or {}
    ranked: list[RankedPlayer] = []
    for entry in board.entries:
        need = needs.tag(entry.position)
        weight = need_weight(need, entry.position)
        if need != NEED_DEPTH and entry.position in urgency_map:
            urgency = max(0.0, min(1.0, urgency_map[entry.position]))
            wait = min(STREAM_WAIT_WEIGHT, weight)
            weight = round(wait + urgency * (weight - wait), 3)
        value = entry.rank_value
        score = value * weight if value > 0 else value
        overlap = bye_counts.get(entry.bye_week, 0) if entry.bye_week else 0
        if score > 0 and overlap > BYE_STACK_FREE:
            penalty = min(
                BYE_PENALTY_MAX, BYE_PENALTY_STEP * (overlap - BYE_STACK_FREE)
            )
            score *= 1.0 - penalty
        ranked.append(
            RankedPlayer(
                entry=entry,
                score=round(score, 2),
                weight=weight,
                need=need,
                bye_overlap=overlap,
                playoff_weeks=tuple(board.playoff_weeks),
                survival=surv.get(entry.canonical_id),
            )
        )

    # Tier cliffs: last available player of their tier at the position.
    by_pos: dict[str, list[RankedPlayer]] = {}
    for rp in ranked:
        by_pos.setdefault(rp.entry.position, []).append(rp)
    for pos_players in by_pos.values():
        pos_players.sort(key=lambda rp: rp.entry.pos_rank)
        for rp, nxt in zip(pos_players, pos_players[1:]):
            if nxt.entry.tier != rp.entry.tier:
                rp.cliff = True
                rp.cliff_drop = round(rp.entry.vorp - nxt.entry.vorp, 1)

    ranked.sort(key=lambda rp: (-rp.score, -rp.entry.vorp, rp.entry.name))
    return ranked


#: Relative spread ``(ceiling − floor) / points`` above which a player reads
#: "high-variance" (boom/bust) and below which "steady" — the middle is just
#: narrated as the range. Sized to the variance model's typical output
#: (established starters ≈ 0.35–0.45, thin-sample players ≈ 0.6+).
WIDE_SPREAD = 0.55
NARROW_SPREAD = 0.38

#: The playoff-weighted season SOS score must sit this far from neutral before
#: it is narrated on its own (the playoff note, when present, already says it).
SOS_NOTE_DELTA = 0.05


def distribution_note(e: BoardEntry) -> str:
    """One line on the floor / ceiling: the range and what kind of bet it is."""
    if e.floor is None or e.ceiling is None or not e.points:
        return ""
    rel = (e.ceiling - e.floor) / e.points
    rng = f"Floor {e.floor:.0f} / median {e.points:.0f} / ceiling {e.ceiling:.0f} pts"
    if rel >= WIDE_SPREAD:
        return f"{rng} — high-variance, upside bet"
    if rel <= NARROW_SPREAD:
        return f"{rng} — steady floor"
    return rng


def _p_wait(rp: RankedPlayer, *, on_the_clock: bool) -> float | None:
    """P(this player survives to the pick *after* the one being decided)."""
    if rp.survival is None:
        return None
    return rp.survival.p_after if on_the_clock else rp.survival.p_next


def lookahead_pick(
    ranked: Sequence[RankedPlayer],
    *,
    picks_until_next: int | None,
    on_the_clock: bool = True,
) -> tuple[RankedPlayer, RankedPlayer | None]:
    """The two-pick lookahead: who to take now, and who (if anyone) it swapped off.

    ``picks_until_next`` is how many picks lie between the pick being decided
    and your following one (drives the fallback ``s_F`` — roughly the
    ``k``-th ranked player is what's left for you then). With no survival
    data or no known next pick the top-scored player stands.
    """
    if not ranked:
        raise ValueError("lookahead_pick needs a non-empty ranking")
    top = ranked[0]
    if picks_until_next is None or len(ranked) < 2:
        return top, None
    p_top = _p_wait(top, on_the_clock=on_the_clock)
    if p_top is None:
        return top, None
    fallback_idx = min(len(ranked) - 1, max(1, picks_until_next))
    s_f = max(0.0, ranked[fallback_idx].score)
    s_a = top.score
    best, best_gain = top, 0.0
    for cand in ranked[1:SWAP_CANDIDATES + 1]:
        p_c = _p_wait(cand, on_the_clock=on_the_clock)
        if p_c is None or cand.score <= 0:
            continue
        if cand.score < SWAP_MIN_SCORE_RATIO * s_a:
            continue
        if not (p_c < COIN_FLIP_BELOW and p_top >= SAFE_ABOVE):
            continue  # only flip when the survival picture is clear-cut
        s_b = cand.score
        ev_top_now = s_a + p_c * s_b + (1.0 - p_c) * s_f
        ev_cand_now = s_b + p_top * s_a + (1.0 - p_top) * s_f
        gain = ev_cand_now - ev_top_now
        if gain > best_gain:
            best, best_gain = cand, gain
    return (best, top) if best is not top else (top, None)


def build_recommendation(
    ranked: Sequence[RankedPlayer],
    needs: RosterNeeds,
    *,
    current_pick: int | None = None,
    picks_until_next: int | None = None,
    on_the_clock: bool = True,
) -> Recommendation | None:
    """Turn the top of the need-weighted ranking into a narrated recommendation.

    ``picks_until_next`` / ``on_the_clock`` feed the survival lookahead
    (upgrade 2); without them the top-scored player is the pick, as before.
    """
    if not ranked:
        return None
    best, swapped_from = lookahead_pick(
        ranked, picks_until_next=picks_until_next, on_the_clock=on_the_clock
    )
    e = best.entry
    blended = e.draft_value is not None and abs(e.draft_value - e.vorp) >= 0.05
    reasons = [
        (
            f"Best weighted value on the board — draft value {e.draft_value:+.1f} "
            f"(season VORP {e.vorp:+.1f}, playoff-blended)"
            if blended
            else f"Best weighted value on the board — VORP {e.vorp:+.1f}"
        )
        + ("" if e.overall_rank == 1 else f", #{e.overall_rank} by raw value")
    ]
    if best.need == NEED_STARTER:
        reasons.append(f"Fills your open {e.position} starting slot")
    elif best.need == NEED_FLEX:
        reasons.append(f"Fits your open flex slot ({e.position})")
    else:
        reasons.append("Starters set here — pure value / depth pick")
    if best.cliff and best.cliff_drop is not None:
        reasons.append(
            f"Last of {e.position} tier {e.tier} — {best.cliff_drop:.0f} pt "
            "cliff to the next tier"
        )
    if e.has_distribution and e.floor is not None and e.ceiling is not None:
        reasons.append(distribution_note(e))
    if e.schedule_note:
        reasons.append(e.schedule_note[0].upper() + e.schedule_note[1:])
    elif e.sos_score is not None and abs(e.sos_score - 1.0) >= SOS_NOTE_DELTA:
        kind = "favorable" if e.sos_score > 1.0 else "difficult"
        reasons.append(
            f"{kind.capitalize()} per-week schedule — {e.sos_score:.2f}× vs "
            f"{e.position} (playoff weeks weighted 2×)"
        )
    if e.injury_note:
        reasons.append(e.injury_note[0].upper() + e.injury_note[1:])
    if best.bye_overlap > BYE_STACK_FREE and e.bye_week is not None:
        reasons.append(
            f"Bye {e.bye_week} already shared by {best.bye_overlap} of your "
            "starters — nudged down, still the pick"
        )
    if e.adp is not None and current_pick is not None and current_pick - e.adp >= 3:
        reasons.append(
            f"Falling value — ADP {e.adp:.0f}, still here at pick {current_pick}"
        )
    if e.value_source != "projection":
        reasons.append(f"Value is {e.value_source}-derived (no stat projection)")
    reasons.extend(_survival_reasons(best, swapped_from, on_the_clock=on_the_clock))
    plan = wait_for(ranked, best, on_the_clock=on_the_clock)
    if plan is not None:
        p = _p_wait(plan, on_the_clock=on_the_clock) or 0.0
        reasons.append(
            f"Plan: {plan.entry.name} ({plan.entry.position}, score {plan.score:.1f}) "
            f"should still be there at your next pick ({p:.0%})"
        )
    return Recommendation(player=best, reasons=reasons, swapped_from=swapped_from)


def wait_for(
    ranked: Sequence[RankedPlayer], best: RankedPlayer, *, on_the_clock: bool
) -> RankedPlayer | None:
    """The best runner-up who is *safe to wait on* — the "plan for next pick".

    The highest-scored player (other than the pick) among the top
    :data:`SWAP_CANDIDATES` + 1 whose survival to your following pick clears
    ``SAFE_ABOVE``; ``None`` when nobody qualifies.
    """
    for rp in ranked[: SWAP_CANDIDATES + 2]:
        if rp is best:
            continue
        p = _p_wait(rp, on_the_clock=on_the_clock)
        if p is not None and p >= SAFE_ABOVE and rp.score > 0:
            return rp
    return None


def _survival_reasons(
    best: RankedPlayer, swapped_from: RankedPlayer | None, *, on_the_clock: bool
) -> list[str]:
    """Narrate the survival picture (and the swap, when the lookahead made one)."""
    out: list[str] = []
    sv = best.survival
    if sv is None:
        return out
    if swapped_from is not None:
        p_b = _p_wait(best, on_the_clock=on_the_clock) or 0.0
        p_a = _p_wait(swapped_from, on_the_clock=on_the_clock) or 0.0
        out.append(
            f"Timing: take now — only {p_b:.0%} to survive to your next pick; "
            f"{swapped_from.entry.name} ({swapped_from.entry.position}, score "
            f"{swapped_from.score:.1f}) should still be there ({p_a:.0%})"
        )
        return out
    if not on_the_clock and sv.p_next is not None:
        if sv.label == LABEL_TAKE_NOW:
            out.append(
                f"Only {sv.p_next:.0%} to reach your pick — have a fallback ready"
            )
        else:
            out.append(f"{sv.p_next:.0%} to still be there at your pick ({sv.label})")
    p_after = sv.p_after
    if p_after is not None and on_the_clock:
        if p_after < TAKE_NOW_BELOW:
            out.append(f"Won't last — {p_after:.0%} to survive to your next pick")
        elif p_after < COIN_FLIP_BELOW:
            out.append(f"Coin flip to survive to your next pick ({p_after:.0%})")
        elif p_after >= SAFE_ABOVE:
            out.append(
                f"Likely there next time too ({p_after:.0%}) — still the best value now"
            )
    if sv.run_excess >= 0.5:
        out.append(f"{best.entry.position} run in progress — availability shaded")
    return out
