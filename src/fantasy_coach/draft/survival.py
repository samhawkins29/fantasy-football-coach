"""Draft-survival probability (framework §4.3): "will he last to my next pick?"

The one question value alone can't answer. Two players may carry similar
need-weighted scores, but if one will certainly still be there in a round and
the other will be gone in three picks, the *order* you take them in decides
whether you get both. This module estimates, per available player, the
probability they are still on the board at your **next** pick and the one
**after**, from ADP as a distribution plus how the room is actually drafting.

The model, in full:

1. **ADP as a distribution.** A player's draft slot ``T`` is treated as
   normal around their ADP with spread ``σ``. Yahoo's ADP feed rarely gives a
   stdev, so the default is the well-known empirical shape — spread grows
   with ADP: ``σ = max(SIGMA_MIN, SIGMA_SLOPE · ADP)`` (a pick-3 player goes
   between 1 and 6; a pick-90 player anywhere from 75 to 105).
2. **Conditional survival.** Given the player is still available at the
   current pick ``c`` (they are — they're on the board), the probability
   they survive the ``k = n − c`` intervening picks to your pick ``n`` is
   ``P(T ≥ n | T ≥ c) = S(n − ½) / S(c − ½)`` with ``S`` the normal survival
   function. This is what makes a player already past their ADP read
   correctly: he *should* have gone, he didn't, and each further pick is a
   fresh chance he does. ``k = 0`` (you are on the clock) → 1.0.
3. **The room's drift.** Real rooms run ahead of or behind the market. The
   median of ``(pick − ADP)`` over the last :data:`DRIFT_WINDOW` made picks
   with an ADP shifts every effective ADP (clamped to :data:`DRIFT_MAX`) —
   a room drafting players a few picks early makes everyone less likely to
   survive.
4. **Positional runs.** When the last :data:`RUN_WINDOW` picks took a
   position more often than the market expected there (the position's share
   of the next ``RUN_WINDOW`` players by ADP at that point), that position's
   effective ADPs are pulled earlier by ``min(σ, RUN_SHIFT_MAX_PICKS) ·
   excess`` (excess capped at 1.0 — a run pulls a player up to a full σ,
   never more than about a round). A run on RB
   makes RBs less likely to survive; the model reacts to what is happening in
   *this* draft, not just the market average.
5. **No ADP.** Players without a market signal (deep sleepers, offline
   stores) fall back to their rank on the *available* board as a pseudo-ADP
   (``current pick − 1 + rank``: the k-th best player left goes about k
   picks from now) with a wider σ — the honest "the market hasn't priced
   him; here's our best guess".

Every output carries a plain-language label (:data:`LABEL_*`) the
recommendation and page use: **take now** (won't survive), **coin flip**,
**likely there**, **safe to wait**.

Live updating: :class:`~fantasy_coach.draft.loop.DraftLoop` recomputes this
each poll from the *current* made-pick list, so the drift and run terms track
the room in real time (framework §4.3 "feed live draftresults in").
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "SIGMA_MIN",
    "SIGMA_SLOPE",
    "SIGMA_NO_ADP",
    "STALE_ADP_HAZARD",
    "DRIFT_WINDOW",
    "DRIFT_MAX",
    "RUN_WINDOW",
    "RUN_SHIFT_SIGMA",
    "RUN_SHIFT_MAX_PICKS",
    "LABEL_TAKE_NOW",
    "LABEL_COIN_FLIP",
    "LABEL_LIKELY",
    "LABEL_SAFE",
    "TAKE_NOW_BELOW",
    "SAFE_ABOVE",
    "Survival",
    "RoomState",
    "adp_sigma",
    "survival_probability",
    "room_state",
    "survival_label",
    "estimate_survival",
]

#: ADP spread floor and slope (σ = max(SIGMA_MIN, SIGMA_SLOPE · ADP)).
SIGMA_MIN = 2.5
SIGMA_SLOPE = 0.12
#: σ for a player with no ADP at all (board rank as pseudo-ADP) — much less
#: certain than a market-priced player.
SIGMA_NO_ADP = 18.0

#: Per-pick chance a player who is many σ past his ADP finally goes (the
#: normal-tail conditional is numerically meaningless out there).
STALE_ADP_HAZARD = 0.15

#: Made picks the room-drift median looks back over, and its clamp (picks).
DRIFT_WINDOW = 24
DRIFT_MAX = 6.0

#: Picks the positional-run detector looks back over, and how far (in σ) a
#: full-strength run pulls that position's effective ADPs earlier.
RUN_WINDOW = 8
RUN_SHIFT_SIGMA = 1.0
#: …but never more than this many picks (a run moves a player up a round at
#: most; the wide no-ADP σ must not swing him twenty picks).
RUN_SHIFT_MAX_PICKS = 8.0

#: Availability labels and their probability cut-offs (P of surviving to
#: your next pick).
LABEL_TAKE_NOW = "take now"
LABEL_COIN_FLIP = "coin flip"
LABEL_LIKELY = "likely there"
LABEL_SAFE = "safe to wait"
TAKE_NOW_BELOW = 0.35
COIN_FLIP_BELOW = 0.65
SAFE_ABOVE = 0.85


@dataclass(slots=True)
class Survival:
    """One player's survival estimate.

    Attributes:
        p_next: P(still available at your next pick). ``None`` when your next
            pick is unknown (draft order not yet observable).
        p_after: P(still available at the pick after that) — same caveat.
        effective_adp: The ADP the model actually used (drift + run applied).
        sigma: The spread used.
        run_excess: The positional-run intensity applied (0 = no run).
        label: The plain-language availability label.
        source: ``"adp"`` or ``"rank"`` (pseudo-ADP fallback).
    """

    p_next: float | None
    p_after: float | None
    effective_adp: float
    sigma: float
    run_excess: float = 0.0
    label: str = ""
    source: str = "adp"

    def as_dict(self) -> dict[str, object]:
        return {
            "p_next": None if self.p_next is None else round(self.p_next, 3),
            "p_after": None if self.p_after is None else round(self.p_after, 3),
            "effective_adp": round(self.effective_adp, 1),
            "run_excess": round(self.run_excess, 2),
            "label": self.label,
            "source": self.source,
        }


@dataclass(slots=True)
class RoomState:
    """What the room is doing right now: drift + per-position run intensity."""

    drift: float = 0.0
    run_excess: dict[str, float] = field(default_factory=dict)
    recent_positions: list[str] = field(default_factory=list)


def _norm_sf(z: float) -> float:
    """Standard normal survival function ``P(Z ≥ z)``."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def adp_sigma(adp: float, stdev: float | None = None) -> float:
    """The spread of a player's draft slot around their ADP.

    A source-reported stdev wins when present (floored at ``SIGMA_MIN``);
    otherwise the empirical slope.
    """
    if stdev is not None and stdev > 0:
        return max(SIGMA_MIN, float(stdev))
    return max(SIGMA_MIN, SIGMA_SLOPE * float(adp))


def survival_probability(
    effective_adp: float, sigma: float, *, current_pick: int, target_pick: int
) -> float:
    """``P(T ≥ target | T ≥ current)`` under ``T ~ N(effective_adp, σ)``.

    ``target_pick <= current_pick`` (you are on the clock, or the target is
    in the past) → 1.0. Monotone non-increasing in ``target_pick`` and
    non-decreasing in ``effective_adp``.
    """
    if target_pick <= current_pick:
        return 1.0
    sigma = max(sigma, 1e-6)
    s_now = _norm_sf((current_pick - 0.5 - effective_adp) / sigma)
    s_then = _norm_sf((target_pick - 0.5 - effective_adp) / sigma)
    if s_now <= 1e-12:
        # Many σ past the ADP: the market's number is stale (news, holdout,
        # a room that simply doesn't rate him) and the normal tail is 0/0.
        # Fall back to a flat per-pick hazard — each intervening pick is an
        # independent chance the slide ends.
        k = target_pick - current_pick
        return (1.0 - STALE_ADP_HAZARD) ** k
    return max(0.0, min(1.0, s_then / s_now))


def room_state(
    made_picks: Sequence[tuple[int, str, float | None]],
    available: Iterable[tuple[str, float | None]],
    *,
    drift_window: int = DRIFT_WINDOW,
    run_window: int = RUN_WINDOW,
) -> RoomState:
    """Measure the room: ADP drift + positional run intensity.

    Args:
        made_picks: ``(pick_number, position, adp)`` for every made pick, in
            order (``adp`` None when the player had none; position "" when
            unmapped).
        available: ``(position, adp)`` for every still-available player —
            the market's expected positional mix for the *next* stretch is
            read off the top of this by ADP.
    """
    made = sorted(made_picks, key=lambda t: t[0])
    recent = made[-drift_window:]
    residuals = sorted(p - adp for p, _, adp in recent if adp is not None)
    drift = 0.0
    if residuals:
        mid = len(residuals) // 2
        drift = (
            residuals[mid]
            if len(residuals) % 2
            else (residuals[mid - 1] + residuals[mid]) / 2.0
        )
        drift = max(-DRIFT_MAX, min(DRIFT_MAX, drift))

    window = [pos for _, pos, _ in made[-run_window:] if pos]
    run_excess: dict[str, float] = {}
    if len(window) >= 3:
        # Market expectation for the same stretch: positional shares among the
        # next `run_window` available players by ADP (unpriced players last).
        pool = sorted(
            ((pos, adp) for pos, adp in available if pos),
            key=lambda t: (t[1] is None, t[1] if t[1] is not None else 0.0),
        )[:run_window]
        if pool:
            expected: dict[str, float] = {}
            for pos, _ in pool:
                expected[pos] = expected.get(pos, 0.0) + 1.0 / len(pool)
            observed: dict[str, float] = {}
            for pos in window:
                observed[pos] = observed.get(pos, 0.0) + 1.0 / len(window)
            for pos, obs in observed.items():
                exp = expected.get(pos, 0.0)
                # Excess share relative to expectation; a position the market
                # expected at 25% going 62% of the time is a full-strength run.
                base = max(exp, 1.0 / run_window)
                excess = (obs - exp) / base if obs > exp else 0.0
                if excess > 0.25:  # ignore ordinary noise
                    run_excess[pos] = min(1.0, excess)
    return RoomState(drift=drift, run_excess=run_excess, recent_positions=window)


def survival_label(p_next: float | None) -> str:
    """The plain-language availability label for a next-pick probability."""
    if p_next is None:
        return ""
    if p_next < TAKE_NOW_BELOW:
        return LABEL_TAKE_NOW
    if p_next < COIN_FLIP_BELOW:
        return LABEL_COIN_FLIP
    if p_next < SAFE_ABOVE:
        return LABEL_LIKELY
    return LABEL_SAFE


def estimate_survival(
    players: Iterable[Mapping[str, object]],
    *,
    current_pick: int,
    my_next_pick: int | None,
    my_pick_after: int | None,
    room: RoomState | None = None,
) -> dict[str, Survival]:
    """Survival estimates for every available player, keyed by canonical id.

    Args:
        players: Mappings with ``canonical_id``, ``position``, ``adp``
            (may be None), ``adp_stdev`` (optional), and ``overall_rank`` (the
            pseudo-ADP fallback).
        current_pick: The pick currently on the clock.
        my_next_pick: Your next pick number (``current_pick`` itself when you
            are on the clock), or ``None`` if unknown.
        my_pick_after: The one after that, or ``None``.
        room: The measured room state (drift + runs); ``None`` = neutral.
    """
    room = room or RoomState()
    out: dict[str, Survival] = {}
    for p in players:
        cid = str(p.get("canonical_id", ""))
        pos = str(p.get("position", "") or "")
        adp = p.get("adp")
        if adp is not None:
            base = float(adp)  # type: ignore[arg-type]
            sigma = adp_sigma(base, p.get("adp_stdev"))  # type: ignore[arg-type]
            source = "adp"
        else:
            # Board rank on the *available* board: the k-th best player left
            # is expected to go about k picks from now.
            rank = float(p.get("overall_rank") or 1)  # type: ignore[arg-type]
            base = current_pick - 1 + rank
            sigma = SIGMA_NO_ADP
            source = "rank"
        excess = room.run_excess.get(pos, 0.0)
        shift = min(RUN_SHIFT_MAX_PICKS, RUN_SHIFT_SIGMA * sigma) * excess
        eff = max(1.0, base + room.drift - shift)
        p_next = p_after = None
        if my_next_pick is not None:
            p_next = survival_probability(
                eff, sigma, current_pick=current_pick, target_pick=my_next_pick
            )
        if my_pick_after is not None:
            p_after = survival_probability(
                eff, sigma, current_pick=current_pick, target_pick=my_pick_after
            )
        out[cid] = Survival(
            p_next=p_next,
            p_after=p_after,
            effective_adp=eff,
            sigma=sigma,
            run_excess=excess,
            label=survival_label(p_next),
            source=source,
        )
    return out
