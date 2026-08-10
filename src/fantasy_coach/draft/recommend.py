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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from fantasy_coach.clients.models import BENCH_POSITIONS, LeagueSettings
from fantasy_coach.ingest.names import normalize_position
from fantasy_coach.value.board import ValueBoard, BoardEntry, _FLEX_CODES

__all__ = [
    "NEED_STARTER",
    "NEED_FLEX",
    "NEED_DEPTH",
    "NEED_WEIGHTS",
    "SlotInstance",
    "roster_slots",
    "assign_roster",
    "RosterNeeds",
    "compute_needs",
    "RankedPlayer",
    "Recommendation",
    "rank_available",
    "build_recommendation",
]

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
        return NEED_WEIGHTS[self.tag(position)]


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
        }


@dataclass(slots=True)
class Recommendation:
    """The single BEST PICK NOW, with the reasons spelled out."""

    player: RankedPlayer
    reasons: list[str] = field(default_factory=list)


def rank_available(
    board: ValueBoard,
    needs: RosterNeeds,
    *,
    starter_bye_counts: Mapping[int, int] | None = None,
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
    """
    bye_counts = starter_bye_counts or {}
    ranked: list[RankedPlayer] = []
    for entry in board.entries:
        need = needs.tag(entry.position)
        weight = NEED_WEIGHTS[need]
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


def build_recommendation(
    ranked: Sequence[RankedPlayer],
    needs: RosterNeeds,
    *,
    current_pick: int | None = None,
) -> Recommendation | None:
    """Turn the top of the need-weighted ranking into a narrated recommendation."""
    if not ranked:
        return None
    best = ranked[0]
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
    if e.schedule_note:
        reasons.append(e.schedule_note[0].upper() + e.schedule_note[1:])
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
    return Recommendation(player=best, reasons=reasons)
