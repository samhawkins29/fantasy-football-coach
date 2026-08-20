"""Live draft state — the drafted set, keeper seeding, and pick resolution (M5).

The one rule of the live loop (framework §7): **Yahoo's draft room is ground
truth**, and every poll returns the *entire* pick list. So the state here is
always **rebuilt from the current made-pick list, never appended to** — a pick
that vanishes between polls (commissioner undo, auto-pick correction) simply
stops being in the list, and the player drops back into the available pool on
the next rebuild. No tombstones, no reconciliation logic, no drift.

Identity joins run on the canonical layer (§3.2): a pick's raw Yahoo id is
resolved through the store's players (``yahoo_id → canonical_id``), with a
direct canonical-id fallback so simulation picks against an offline-warmed
store (whose synthesized rows carry no yahoo ids) resolve too. An id that
resolves nowhere is still *counted and excluded by its raw id* — the pick
happened whether or not we can name the player — and logged once, not every
2.5 seconds.

Keepers and pre-draft rosters are seeded separately from picks (they never
appear in ``draftresults``) and survive every rebuild.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from fantasy_coach.clients.models import DraftPick
from fantasy_coach.ingest.canonical import CanonicalPlayer

__all__ = [
    "OFF_BOARD_PREFIX",
    "ResolvedPick",
    "DraftState",
    "off_board_id",
    "parse_off_board_id",
    "snake_team_for_pick",
]

logger = logging.getLogger(__name__)

#: Raw-id prefix for **off-board picks** — a player who exists in the real
#: draft room but not in our store (a deep rookie, an obscure DEF). The id
#: encodes the position (and an optional label) so the pick still consumes the
#: right roster slot and the needs/survival math stays correct:
#: ``OFF:LB:Some Guy``. These ids resolve to no canonical player on purpose.
OFF_BOARD_PREFIX = "OFF:"


def off_board_id(position: str, label: str = "") -> str:
    """Build an off-board raw id (``OFF:{POS}:{label}``).

    Dots are stripped from the label — the id travels inside a Yahoo-shaped
    ``player_key`` (``{game}.p.{id}``) whose parser splits on dots.
    """
    clean_label = (label or "").replace(".", " ").strip()
    return f"{OFF_BOARD_PREFIX}{(position or '').strip().upper()}:{clean_label}"


def parse_off_board_id(raw_id: str) -> tuple[str, str] | None:
    """``(position, label)`` from an off-board raw id, else ``None``."""
    if not raw_id.startswith(OFF_BOARD_PREFIX):
        return None
    rest = raw_id[len(OFF_BOARD_PREFIX):]
    position, _, label = rest.partition(":")
    return position.strip().upper(), label.strip()


@dataclass(slots=True)
class ResolvedPick:
    """One *made* pick joined to the canonical layer.

    ``canonical_id`` is ``None`` when the raw Yahoo id resolved nowhere — the
    pick still removes a body from the pool and occupies a roster spot, we just
    can't say whose.
    """

    pick: DraftPick
    canonical_id: str | None

    @property
    def raw_id(self) -> str:
        """The bare id Yahoo reported (``player_key`` tail)."""
        return self.pick.player_id


class DraftState:
    """The rebuilt-every-poll picture of who is gone and who is mine.

    Args:
        players: The canonical player universe (``store.canonical_players()``)
            — builds the ``yahoo_id → canonical_id`` resolution map and the
            direct canonical-id fallback set.
        league_key: The league being drafted.
        my_team_key: The founder's team; picks with this ``team_key`` feed the
            roster-need weighting. May be set later via :attr:`my_team_key`.
    """

    def __init__(
        self,
        players: Iterable[CanonicalPlayer],
        *,
        league_key: str,
        my_team_key: str = "",
    ) -> None:
        self.league_key = league_key
        self.my_team_key = my_team_key
        self._by_yahoo: dict[str, str] = {}
        self._canonical_ids: set[str] = set()
        for p in players:
            self._canonical_ids.add(p.canonical_id)
            if p.ids.yahoo_id:
                self._by_yahoo[str(p.ids.yahoo_id)] = p.canonical_id
        # team_key -> resolved keeper canonical ids (order preserved for roster)
        self._keepers: dict[str, list[str]] = {}
        self._keeper_unmapped: dict[str, list[str]] = {}
        self._logged_unmapped: set[str] = set()
        self.resolved: list[ResolvedPick] = []
        #: raw id -> pick number for made picks that resolved nowhere (0 = keeper)
        self.unmapped: dict[str, int] = {}

    # -- id resolution -------------------------------------------------------

    def resolve_raw_id(self, raw_id: str) -> str | None:
        """Resolve a bare Yahoo player id (or canonical id) to a canonical id.

        Tries the yahoo crosswalk first, then a direct canonical-id match (the
        simulation / offline-store path). ``None`` means unmapped.
        """
        if raw_id in self._by_yahoo:
            return self._by_yahoo[raw_id]
        if raw_id in self._canonical_ids:
            return raw_id
        return None

    def _log_unmapped(self, raw_id: str, where: str) -> None:
        if raw_id.startswith(OFF_BOARD_PREFIX):
            return  # deliberate off-board entry — not a resolution failure
        if raw_id not in self._logged_unmapped:
            self._logged_unmapped.add(raw_id)
            logger.warning(
                "unmapped player id %r (%s) — excluding by raw id; the pick "
                "counts but carries no board identity",
                raw_id,
                where,
            )

    # -- keepers / pre-draft rosters ------------------------------------------

    def seed_keepers(self, team_key: str, raw_ids: Iterable[str]) -> int:
        """Seed a team's pre-rostered players (keepers) as already drafted.

        Call once per team before the loop starts (from ``get_league_teams`` +
        ``get_team_roster``). Returns how many ids were seeded. Keepers survive
        every :meth:`rebuild` — they are not part of ``draftresults``.
        """
        resolved = self._keepers.setdefault(team_key, [])
        unmapped = self._keeper_unmapped.setdefault(team_key, [])
        count = 0
        for raw in raw_ids:
            raw = str(raw)
            if not raw:
                continue
            canonical = self.resolve_raw_id(raw)
            if canonical is None:
                self._log_unmapped(raw, f"keeper on {team_key}")
                self.unmapped.setdefault(raw, 0)
                unmapped.append(raw)
            elif canonical not in resolved:
                resolved.append(canonical)
            count += 1
        return count

    @property
    def keeper_canonical_ids(self) -> set[str]:
        """Every seeded keeper across all teams (canonical ids only)."""
        return {cid for ids in self._keepers.values() for cid in ids}

    # -- the rebuild ----------------------------------------------------------

    def rebuild(self, picks: Sequence[DraftPick]) -> None:
        """Rebuild the drafted picture from the *current* full pick list.

        Rebuild, not append: a previously seen pick missing from ``picks`` (an
        undo or auto-pick correction) is simply gone, and its player returns to
        the pool. Unmapped made picks are re-derived too, so an id that becomes
        resolvable later (players table refreshed) heals itself.
        """
        made = sorted((p for p in picks if p.is_made), key=lambda p: p.pick)
        self.resolved = []
        self.unmapped = {raw: 0 for team in self._keeper_unmapped.values() for raw in team}
        for pick in made:
            canonical = self.resolve_raw_id(pick.player_id)
            if canonical is None:
                self._log_unmapped(pick.player_id, f"pick #{pick.pick}")
                self.unmapped[pick.player_id] = pick.pick
            self.resolved.append(ResolvedPick(pick=pick, canonical_id=canonical))

    # -- views the loop reads --------------------------------------------------

    @property
    def pick_count(self) -> int:
        """How many picks have actually been made (keepers excluded)."""
        return len(self.resolved)

    @property
    def drafted_canonical_ids(self) -> set[str]:
        """Everyone gone from the pool: keepers ∪ made picks (canonical ids)."""
        gone = self.keeper_canonical_ids
        gone.update(rp.canonical_id for rp in self.resolved if rp.canonical_id)
        return gone

    @property
    def drafted_raw_ids(self) -> set[str]:
        """Raw ids of *unmapped* drafted players — excluded even without identity."""
        return set(self.unmapped)

    @property
    def my_picks(self) -> list[ResolvedPick]:
        """The founder's made picks, in pick order."""
        return [rp for rp in self.resolved if rp.pick.team_key == self.my_team_key]

    def my_canonical_ids(self) -> list[str]:
        """The founder's roster so far: keepers first, then picks in order."""
        mine = list(self._keepers.get(self.my_team_key, []))
        for rp in self.my_picks:
            if rp.canonical_id and rp.canonical_id not in mine:
                mine.append(rp.canonical_id)
        return mine

    def keeper_teams(self) -> list[str]:
        """Team keys that had keepers seeded."""
        return list(self._keepers)

    def team_acquisitions(
        self, team_key: str
    ) -> list[tuple[str | None, bool, int | None, str]]:
        """A team's roster so far as ``(canonical_id, unmapped, pick_no, raw_id)``:
        seeded keepers first (pick None), then its made picks in pick order.
        ``raw_id`` lets callers recover position/label for unmapped off-board
        picks (:func:`parse_off_board_id`).
        """
        out: list[tuple[str | None, bool, int | None, str]] = [
            (cid, False, None, cid) for cid in self._keepers.get(team_key, [])
        ]
        out.extend(
            (None, True, None, raw)
            for raw in self._keeper_unmapped.get(team_key, [])
        )
        for rp in self.resolved:
            if rp.pick.team_key == team_key:
                out.append(
                    (rp.canonical_id, rp.canonical_id is None, rp.pick.pick, rp.raw_id)
                )
        return out

    def my_unmapped_count(self) -> int:
        """My picks/keepers that resolved nowhere (they still occupy slots)."""
        keeper_unmapped = len(self._keeper_unmapped.get(self.my_team_key, []))
        return keeper_unmapped + sum(
            1 for rp in self.my_picks if rp.canonical_id is None
        )


# --------------------------------------------------------------------------- #
# Snake-order prediction (who is on the clock / when am I up again)
# --------------------------------------------------------------------------- #


def snake_team_for_pick(
    round1_order: Sequence[str], pick_number: int
) -> str | None:
    """The team on the clock at ``pick_number`` in a snake draft.

    ``round1_order`` is the observed team order of round 1 (made picks 1..N);
    until round 1 completes the order is unknown and this returns ``None``.
    """
    n = len(round1_order)
    if n == 0 or pick_number < 1:
        return None
    rnd, idx = divmod(pick_number - 1, n)
    return round1_order[idx] if rnd % 2 == 0 else round1_order[n - 1 - idx]
