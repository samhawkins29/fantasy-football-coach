"""Manual draft entry — the guaranteed offline path for a live Yahoo draft.

Yahoo's API approval may never come. This module makes the companion fully
usable anyway: the founder marks each pick as it happens in the draft room
and the *same* :class:`~fantasy_coach.draft.loop.DraftLoop` — the same
undo-safe rebuild, value board, need weighting, survival math, page — runs
off that pick stream. Yahoo auto-sync, if it is ever approved, is an optional
overlay that fills the identical stream (:meth:`ManualPickSource.overlay`).

Pieces:

* :class:`ManualPickSource` — a full Yahoo-shaped pick list (every pick of
  the snake draft with its team key prefilled from the round-1 order; keeper
  picks pre-made) that the founder fills in pick by pick: :meth:`mark`,
  :meth:`unmark`, :meth:`undo`. It is a :class:`~fantasy_coach.draft.loop.PickSource`,
  so ``DraftLoop`` polls it like Yahoo. It **persists** through the loop's
  store mirror (``draft_picks``): the loop rewrites the made picks after
  every change, and :meth:`ManualPickSource.restore` reloads them at start —
  an accidental refresh / crash mid-draft loses nothing.
* :class:`PlayerFinder` — search-as-you-type over the store's players
  (fuzzy: prefix, subsequence, initials, transpositions), available players
  first, board order within a match tier. No dependency beyond the stdlib.
* :class:`ManualDraft` — the controller the web layer talks to: search,
  mark (defaults the team to whoever is on the clock and the pick to the
  first unmade slot), unmark, undo, reset — each mutation triggers an
  immediate loop recompute so the page updates on the very next fetch.

The founder's slot is a first-class input (``my_slot``) so the loop knows
which picks are theirs from pick 1 and the survival model can say "your pick
is in N; these are likely gone by then".
"""

from __future__ import annotations

import difflib
import logging
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from fantasy_coach.clients.models import DraftPick
from fantasy_coach.ingest.names import clean_name

__all__ = [
    "snake_team_keys",
    "ManualPickSource",
    "PlayerFinder",
    "ManualDraft",
]

logger = logging.getLogger(__name__)


def snake_team_keys(order: Sequence[str], rounds: int) -> list[str]:
    """Team key per pick number (1-based list index 0 = pick 1) for a snake draft."""
    n = len(order)
    out: list[str] = []
    for rnd in range(rounds):
        row = list(order) if rnd % 2 == 0 else list(reversed(order))
        out.extend(row)
    return out[: n * rounds]


class ManualPickSource:
    """A pick list the founder fills in by hand (see the module docstring).

    Args:
        order: Team keys in round-1 order.
        rounds: Draft length.
        game_code: Prefix for scripted ``player_key``s (``"{game}.p.{id}"``).
        keepers: ``(team_key, round, canonical_id)`` — pre-made picks.
    """

    def __init__(
        self,
        order: Sequence[str],
        rounds: int,
        *,
        game_code: str = "manual",
        keepers: Iterable[tuple[str, int, str]] = (),
    ) -> None:
        self.order = list(order)
        self.rounds = rounds
        self.num_teams = len(self.order)
        self._game = game_code
        teams = snake_team_keys(self.order, rounds)
        self._picks: list[DraftPick] = [
            DraftPick(pick=i + 1, round=i // self.num_teams + 1, team_key=teams[i])
            for i in range(len(teams))
        ]
        self._history: list[int] = []  # pick numbers in the order they were marked
        self._keeper_picks: set[int] = set()
        self._overlay = None
        for team_key, rnd, cid in keepers:
            pick_no = self.pick_number_for(team_key, rnd)
            if pick_no is not None:
                self._set(pick_no, cid, keeper=True)

    # -- geometry --------------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self._picks)

    def pick_number_for(self, team_key: str, round_no: int) -> int | None:
        """The pick number ``team_key`` owns in ``round_no`` (snake), or None."""
        for p in self._picks:
            if p.round == round_no and p.team_key == team_key:
                return p.pick
        return None

    def team_for(self, pick_no: int) -> str:
        return self._picks[pick_no - 1].team_key if 1 <= pick_no <= self.total else ""

    def next_open(self) -> int | None:
        """The first unmade pick number (the pick on the clock), or None if done."""
        for p in self._picks:
            if not p.is_made:
                return p.pick
        return None

    def made_picks(self) -> list[DraftPick]:
        return [p for p in self._picks if p.is_made]

    def picks_by_player(self) -> dict[str, int]:
        """``{raw player id: pick number}`` for every made pick."""
        return {p.player_id: p.pick for p in self._picks if p.is_made}

    # -- mutation --------------------------------------------------------------

    def _set(self, pick_no: int, raw_id: str, *, team_key: str | None = None, keeper: bool = False) -> DraftPick:
        p = self._picks[pick_no - 1]
        p.player_key = f"{self._game}.p.{raw_id}"
        if team_key:
            p.team_key = team_key
        if keeper:
            self._keeper_picks.add(pick_no)
        return p

    def mark(
        self, raw_id: str, *, team_key: str | None = None, pick_no: int | None = None
    ) -> DraftPick:
        """Record that ``raw_id`` was drafted.

        Defaults: the first open pick (whoever is on the clock) — pass
        ``team_key`` when the room's order differs from ours (a trade) or to
        assign a specific team, and ``pick_no`` to fill a specific slot.
        A player already marked elsewhere is moved (never duplicated).
        Raises ``ValueError`` when the draft is complete or the slot is taken.
        """
        raw_id = str(raw_id)
        prior = self.picks_by_player().get(raw_id)
        if prior is not None:
            self.unmark(prior)
        if pick_no is None:
            pick_no = self.next_open()
            if pick_no is None:
                raise ValueError("draft complete — no open pick to mark")
        if not 1 <= pick_no <= self.total:
            raise ValueError(f"pick {pick_no} is outside 1..{self.total}")
        if self._picks[pick_no - 1].is_made:
            raise ValueError(f"pick {pick_no} is already made — unmark it first")
        p = self._set(pick_no, raw_id, team_key=team_key)
        self._history.append(pick_no)
        return p

    def unmark(self, pick_no: int) -> DraftPick | None:
        """Blank a made pick (a mis-entry); returns it, or None if it was open."""
        if not 1 <= pick_no <= self.total:
            return None
        p = self._picks[pick_no - 1]
        if not p.is_made:
            return None
        removed = DraftPick(pick=p.pick, round=p.round, team_key=p.team_key, player_key=p.player_key)
        p.player_key = ""
        # Team key stays as the schedule says (a manual override is kept:
        # the slot's owner doesn't change because the entry was wrong).
        self._keeper_picks.discard(pick_no)
        self._history = [n for n in self._history if n != pick_no]
        return removed

    def undo(self) -> DraftPick | None:
        """Unmark the most recently *marked* pick (not the highest number)."""
        if not self._history:
            return None
        return self.unmark(self._history[-1])

    def reset(self) -> None:
        """Blank every non-keeper pick."""
        for p in self._picks:
            if p.pick not in self._keeper_picks:
                p.player_key = ""
        self._history = []

    def restore(self, picks: Iterable[DraftPick]) -> int:
        """Reload made picks (from the store after a restart). Returns count."""
        n = 0
        for p in sorted(picks, key=lambda x: x.pick):
            if p.is_made and 1 <= p.pick <= self.total and not self._picks[p.pick - 1].is_made:
                self._set(p.pick, p.player_id, team_key=p.team_key or None)
                if p.pick not in self._keeper_picks:
                    self._history.append(p.pick)
                n += 1
        return n

    # -- optional Yahoo overlay ------------------------------------------------

    def overlay(self, source) -> None:
        """Layer another :class:`PickSource` (Yahoo) over the manual stream.

        On every :meth:`fetch`, made picks from the overlay fill any slot the
        founder hasn't marked (the auto-sync bonus); manual entries always
        win. Overlay failures are logged and ignored — manual mode must
        never depend on it.
        """
        self._overlay = source

    # -- PickSource -------------------------------------------------------------

    def fetch(self) -> list[DraftPick]:
        if self._overlay is not None:
            try:
                for p in self._overlay.fetch():
                    if p.is_made and 1 <= p.pick <= self.total and not self._picks[p.pick - 1].is_made:
                        self._set(p.pick, p.player_id, team_key=p.team_key or None)
            except Exception as exc:  # the overlay is a bonus, never a dependency
                logger.warning("pick overlay failed (%s) — manual stream stands", exc)
        return [
            DraftPick(pick=p.pick, round=p.round, team_key=p.team_key, player_key=p.player_key)
            for p in self._picks
        ]


# --------------------------------------------------------------------------- #
# Search-as-you-type
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Indexed:
    canonical_id: str
    name: str
    clean: str
    tokens: tuple[str, ...]
    initials: str
    position: str
    team: str
    rank: int
    raw_id: str


class PlayerFinder:
    """Fuzzy player lookup tuned for a 1:45 clock.

    Match tiers (best first): whole-name prefix ("jah gib" → Jahmyr Gibbs),
    every query token a prefix of some name token in order ("gibbs" / "j
    gibbs"), initials ("cmc"), then fuzzy (difflib ratio ≥ 0.72 — catches
    "mcafrey"). Within a tier, available players come first, then board
    order. Results carry ``available`` and the pick number when taken.
    """

    def __init__(self, players: Iterable[Mapping[str, object]]) -> None:
        self._items: list[_Indexed] = []
        for p in players:
            name = str(p.get("name", "") or "")
            if not name:
                continue
            clean = clean_name(name)
            tokens = tuple(clean.split())
            self._items.append(
                _Indexed(
                    canonical_id=str(p["canonical_id"]),
                    name=name,
                    clean=clean,
                    tokens=tokens,
                    initials="".join(t[0] for t in tokens),
                    position=str(p.get("position", "") or ""),
                    team=str(p.get("team", "") or ""),
                    rank=int(p.get("overall_rank") or 10_000),
                    raw_id=str(p.get("raw_id") or p["canonical_id"]),
                )
            )

    @staticmethod
    def _tier(item: _Indexed, q: str, q_tokens: tuple[str, ...]) -> int | None:
        if item.clean.startswith(q):
            return 0
        if q_tokens and all(any(t.startswith(qt) for t in item.tokens) for qt in q_tokens):
            # in-order token prefixes ("j gib" but not "gib j")
            pos = 0
            ok = True
            for qt in q_tokens:
                nxt = next((i for i in range(pos, len(item.tokens)) if item.tokens[i].startswith(qt)), None)
                if nxt is None:
                    ok = False
                    break
                pos = nxt + 1
            # A single-token query ("chase") matches Ja'Marr Chase and Chase
            # Brown equally well — let board rank decide, don't privilege the
            # whole-name prefix. Multi-token queries keep the order signal.
            return (0 if len(q_tokens) == 1 else 1) if ok else 2
        compact = q.replace(" ", "")
        if len(compact) >= 2 and item.initials.startswith(compact):
            return 3
        # "cmc"-style: first initial + a prefix of the last name.
        if (
            len(compact) >= 3 and len(item.tokens) >= 2
            and compact[0] == item.tokens[0][0] and item.tokens[-1].startswith(compact[1:])
        ):
            return 3
        if len(q) >= 4:
            ratio = difflib.SequenceMatcher(None, q, item.clean[: len(q) + 3]).ratio()
            if ratio >= 0.72:
                return 4
            last = item.tokens[-1] if item.tokens else ""
            if last and difflib.SequenceMatcher(None, q, last).ratio() >= 0.75:
                return 4
        return None

    def search(
        self,
        query: str,
        *,
        taken: Mapping[str, int] | None = None,
        limit: int = 8,
        position: str = "",
    ) -> list[dict[str, object]]:
        q = clean_name(query)
        if not q:
            return []
        q_tokens = tuple(q.split())
        taken = taken or {}
        scored: list[tuple[int, int, int, _Indexed]] = []
        for item in self._items:
            if position and item.position != position:
                continue
            tier = self._tier(item, q, q_tokens)
            if tier is None:
                continue
            gone = 1 if item.raw_id in taken or item.canonical_id in taken else 0
            scored.append((tier, gone, item.rank, item))
        scored.sort(key=lambda t: (t[0], t[1], t[2], t[3].name))
        out = []
        for tier, gone, rank, item in scored[:limit]:
            pick_no = taken.get(item.raw_id, taken.get(item.canonical_id))
            out.append(
                {
                    "canonical_id": item.canonical_id,
                    "raw_id": item.raw_id,
                    "name": item.name,
                    "position": item.position,
                    "team": item.team,
                    "rank": rank if rank < 10_000 else None,
                    "available": not gone,
                    "pick": pick_no,
                }
            )
        return out


# --------------------------------------------------------------------------- #
# The controller the web layer drives
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ManualDraft:
    """Glue between the web layer, the pick source and the loop.

    Every mutation calls ``loop.poll_once()`` so the board recomputes at
    once (the loop's own poll thread keeps running too; ``poll_once`` is
    serialized inside the loop).
    """

    source: ManualPickSource
    loop: object  # DraftLoop (typed loosely to avoid an import cycle)
    finder: PlayerFinder
    team_names: dict[str, str] = field(default_factory=dict)

    def search(self, query: str, *, limit: int = 8, position: str = "") -> list[dict[str, object]]:
        return self.finder.search(
            query, taken=self.source.picks_by_player(), limit=limit, position=position
        )

    def mark(self, raw_id: str, *, team_key: str = "", pick_no: int | None = None) -> dict[str, object]:
        pick = self.source.mark(raw_id, team_key=team_key or None, pick_no=pick_no)
        return self._after(f"marked pick {pick.pick} ({self.team_names.get(pick.team_key, pick.team_key)})")

    def unmark(self, pick_no: int) -> dict[str, object]:
        removed = self.source.unmark(pick_no)
        return self._after(
            f"cleared pick {pick_no}" if removed else f"pick {pick_no} was already open"
        )

    def undo(self) -> dict[str, object]:
        removed = self.source.undo()
        return self._after(f"undid pick {removed.pick}" if removed else "nothing to undo")

    def reset(self) -> dict[str, object]:
        self.source.reset()
        return self._after("draft reset (keepers kept)")

    def _after(self, message: str) -> dict[str, object]:
        snap = self.loop.poll_once()  # type: ignore[attr-defined]
        return {"ok": True, "message": message, "state": snap}

    def teams(self) -> list[dict[str, object]]:
        """Round-1 order with display names (for the team picker)."""
        return [
            {"team_key": k, "name": self.team_names.get(k, k.rsplit(".", 1)[-1])}
            for k in self.source.order
        ]
