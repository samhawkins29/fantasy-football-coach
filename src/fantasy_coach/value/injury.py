"""Injury-aware valuation: status + durability discounts on draft value (step 6).

The valuation counterpart of :mod:`fantasy_coach.ingest.injury`, mirroring the
step-5 split (schedule data in ingest, schedule math here). Everything is a
**nudge dial**, off-by-default-safe: at ``injury_weight = 0`` the board's
values and ordering are bit-identical to the pre-step-6 board — flags and
notes still appear (visibility is free), only the *ranking* is gated.

**Two layers** (P0-5 split): hard designations (O/SUS/PUP/NFI/IR) carry an
**always-on availability haircut** (:func:`availability_haircut`) — missing
games is a fact of the designation, not a risk preference, so it applies to
draft value regardless of the dial, and a reserve-list stint with a
season-ending detail (Achilles/ACL/patellar) caps the value near zero. The
**dial** below covers only soft risk: day-to-day designations and durability
history.

The dial model, in full (no hidden steps — every output is a model estimate):

1. **Status discount** (:data:`STATUS_DISCOUNTS`): Questionable/Doubtful map
   to small documented haircuts — most Questionables play, so whether they
   shade value at all is the founder's choice via the dial.
2. **Durability discount**: the clamped games-missed signal from
   :class:`~fantasy_coach.ingest.injury.DurabilityProfile` (already ≤ 0.15).
3. **Playoff tie-in** (step 5 → step 6): an injury-prone player is a bigger
   gamble for a roster built to peak in weeks 15–17 — the *durability* term is
   scaled by ``1 + PLAYOFF_RISK_FACTOR × playoff_weight``, so turning the
   playoff dial up makes chronic risk weigh slightly more. Current status is
   not scaled: today's Questionable says little about week 16.
4. **Combine + clamp + weight**: ``total = min(cap, status + scaled
   durability)``; the applied multiplier is ``1 − injury_weight × total``.
   Only positive values are shaded (a below-replacement player is not
   *improved* by being hurt), matching the need-weighting philosophy.

Honesty: re-injury cannot be predicted. The note strings narrate exactly what
was observed ("missed 12 games over 2 seasons") and what was done ("value
shaded 8%"), never a recovery forecast.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fantasy_coach.ingest.injury import (
    STATUS_HEALTHY,
    DurabilityProfile,
    InjuryReport,
    RISK_ELEVATED,
    RISK_HIGH,
)

__all__ = [
    "STATUS_DISCOUNTS",
    "AVAILABILITY_HAIRCUTS",
    "SEASON_ENDING_DETAILS",
    "SEASON_ENDING_HAIRCUT",
    "PLAYOFF_RISK_FACTOR",
    "TOTAL_DISCOUNT_CAP",
    "PlayerRisk",
    "build_risk_index",
    "availability_haircut",
    "total_discount",
    "injury_multiplier",
    "injury_note",
]

#: Soft-risk discount per normalized status code — the fraction of a player's
#: positive value shaded at full injury weight (the **dial-gated** layer:
#: day-to-day designations say little about the season, so whether they shade
#: value at all is the founder's choice). Hard designations live in
#: :data:`AVAILABILITY_HAIRCUTS` instead — they are applied regardless of the
#: dial. A designation we can't read (not listed) discounts nothing.
STATUS_DISCOUNTS: dict[str, float] = {
    STATUS_HEALTHY: 0.0,
    "Q": 0.05,  # mild haircut — most Questionables play
    "D": 0.15,
}

#: Draft-time **expected-games availability haircut** per hard designation —
#: applied to draft value REGARDLESS of the injury dial (P0-5). Unlike the
#: soft dial, these designations carry factual availability costs: a PUP/NFI
#: stint is ≥4 missed weeks by rule, an August IR usually costs half a season.
#: Documented model estimates of the season-value fraction lost.
AVAILABILITY_HAIRCUTS: dict[str, float] = {
    "SUS": 0.20,  # suspension length unknown; often multi-week
    "O": 0.25,
    "NA": 0.35,
    "PUP": 0.45,  # reserve lists: out ≥4 weeks by rule, return uncertain
    "NFI": 0.45,
    "IR": 0.55,
}

#: Injury details that, combined with a reserve-list designation, read
#: season-ending (or season-wrecking) at draft time — an August PUP with a
#: torn Achilles is not a "miss a month" stint. Matched case-insensitively as
#: substrings of the report detail.
SEASON_ENDING_DETAILS: tuple[str, ...] = ("achilles", "acl", "patellar")

#: The haircut for a reserve-list designation with a season-ending detail —
#: near zero, not zero: a late-season return or a cheap stash keeps a sliver
#: of draft value at the very end of the board.
SEASON_ENDING_HAIRCUT = 0.9

#: Designations that put a player on a reserve list (out for weeks by rule) —
#: the ones a season-ending detail escalates.
_RESERVE_STATUSES = frozenset({"PUP", "NFI", "IR", "O"})

#: How much the playoff dial amplifies the durability term: at playoff weight
#: ``w`` the durability discount is scaled by ``1 + w × this``. A nudge on a
#: nudge — at the recommended w≈0.3 tilt it adds 15% to the durability share.
PLAYOFF_RISK_FACTOR = 0.5

#: Ceiling on the combined (status + durability) discount before weighting —
#: even an IR player with a chronic history keeps 40%+ of their value on the
#: board, because value is why they'd be stashed at all.
TOTAL_DISCOUNT_CAP = 0.60


@dataclass(slots=True)
class PlayerRisk:
    """One player's merged injury picture: current status + durability history."""

    report: InjuryReport | None = None
    durability: DurabilityProfile | None = None

    @property
    def status(self) -> str:
        """The normalized current-status code (healthy when no report)."""
        return self.report.status if self.report is not None else STATUS_HEALTHY

    @property
    def status_discount(self) -> float:
        """The documented haircut for the current designation."""
        return STATUS_DISCOUNTS.get(self.status, 0.0)

    @property
    def durability_risk(self) -> str:
        """The categorical durability flag (empty when no history profile)."""
        return self.durability.risk if self.durability is not None else ""

    @property
    def is_flagged(self) -> bool:
        """True when there is anything worth surfacing at all."""
        return self.status != STATUS_HEALTHY or self.durability_risk in (
            RISK_HIGH,
            RISK_ELEVATED,
        )


def build_risk_index(
    reports: Mapping[str, InjuryReport] | None = None,
    durability: Mapping[str, DurabilityProfile] | None = None,
) -> dict[str, PlayerRisk]:
    """Join merged status reports + durability profiles into one per-player index.

    The board consumes this: ``{canonical_id: PlayerRisk}``. Either input may
    be absent — a player can have a status with no history (rookies) or a
    history with no current designation (most veterans).
    """
    index: dict[str, PlayerRisk] = {}
    for cid, report in (reports or {}).items():
        index[cid] = PlayerRisk(report=report)
    for cid, profile in (durability or {}).items():
        if cid in index:
            index[cid].durability = profile
        else:
            index[cid] = PlayerRisk(durability=profile)
    return index


def availability_haircut(risk: PlayerRisk) -> float:
    """The always-on expected-games haircut for a hard designation (P0-5).

    ``0.0`` for healthy / day-to-day players (their soft risk is the dial's
    business). A reserve-list designation whose detail reads season-ending
    (:data:`SEASON_ENDING_DETAILS` — Achilles, ACL, patellar) escalates to
    :data:`SEASON_ENDING_HAIRCUT`.
    """
    haircut = AVAILABILITY_HAIRCUTS.get(risk.status, 0.0)
    if haircut <= 0.0:
        return 0.0
    detail = (risk.report.detail if risk.report is not None else "").lower()
    if risk.status in _RESERVE_STATUSES and any(
        key in detail for key in SEASON_ENDING_DETAILS
    ):
        return SEASON_ENDING_HAIRCUT
    return haircut


def total_discount(risk: PlayerRisk, *, playoff_weight: float = 0.0) -> float:
    """The combined, clamped availability discount before injury weighting.

    ``min(cap, status + durability × (1 + PLAYOFF_RISK_FACTOR × w))`` — see the
    module docstring for why only the durability term sees the playoff dial.
    """
    durability = risk.durability.discount if risk.durability is not None else 0.0
    scaled = durability * (1.0 + PLAYOFF_RISK_FACTOR * max(0.0, playoff_weight))
    return round(min(TOTAL_DISCOUNT_CAP, risk.status_discount + scaled), 4)


def injury_multiplier(
    risk: PlayerRisk, *, weight: float, playoff_weight: float = 0.0
) -> float:
    """The value multiplier: ``1 − weight × total_discount``.

    ``weight = 0`` returns exactly 1.0 — the off-by-default guarantee.
    """
    return 1.0 - weight * total_discount(risk, playoff_weight=playoff_weight)


def injury_note(risk: PlayerRisk, *, shaded_pct: float | None = None) -> str:
    """A short, honest narration of the risk picture (board + BEST PICK NOW).

    Speaks only when there is something to say: a current designation
    ("QUESTIONABLE (knee) — monitor [sleeper]") and/or a high/elevated
    durability flag ("high re-injury risk — missed 12 games over 2 seasons").
    ``shaded_pct`` (0–100) appends what the discount actually did to the
    player's board value, so a shaded rank is never silent.
    """
    parts: list[str] = []
    report = risk.report
    if report is not None and report.status != STATUS_HEALTHY:
        text = report.label
        if report.detail:
            text += f" ({report.detail.lower()})"
        if report.status == "Q":
            text += " — monitor"
        if report.source:
            text += f" [{report.source}]"
        parts.append(text)
    profile = risk.durability
    if profile is not None and profile.risk in (RISK_HIGH, RISK_ELEVATED):
        missed = profile.total_missed
        span = profile.seasons_seen
        parts.append(
            f"{profile.risk} re-injury risk — missed {missed:.0f} games over "
            f"{span} season{'s' if span != 1 else ''} (model estimate)"
        )
    if parts and shaded_pct is not None and shaded_pct > 0:
        parts.append(f"value shaded {shaded_pct:.0f}%")
    return " · ".join(parts)
