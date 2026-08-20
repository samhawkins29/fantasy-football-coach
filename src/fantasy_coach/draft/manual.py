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
from fantasy_coach.draft.state import off_board_id
from fantasy_coach.ingest.names import clean_name, normalize_position
from fantasy_coach.league import KeeperConflict, KeeperRules, assign_keeper_rounds

__all__ = [
    "snake_team_keys",
    "ManualPickSource",
    "PlayerFinder",
    "KeeperBook",
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

    def apply_keepers(self, keepers: Iterable[tuple[str, int, str]]) -> list[str]:
        """Replace the pre-made keeper picks with ``(team_key, round, canonical_id)``.

        Previous keeper picks are blanked, new ones pre-made. A keeper whose
        cost-round slot is already a *live* pick is reported (not applied)
        — clear that pick first. Returns the warnings.
        """
        for pick_no in sorted(self._keeper_picks):
            p = self._picks[pick_no - 1]
            p.player_key = ""
        self._keeper_picks.clear()
        warnings: list[str] = []
        for team_key, rnd, cid in keepers:
            pick_no = self.pick_number_for(team_key, int(rnd))
            if pick_no is None:
                warnings.append(f"{team_key} has no round {rnd} in a {self.rounds}-round draft")
                continue
            if self._picks[pick_no - 1].is_made:
                warnings.append(f"pick {pick_no} ({team_key} round {rnd}) already holds a live pick")
                continue
            prior = self.picks_by_player().get(str(cid))
            if prior is not None:
                self.unmark(prior)  # was marked live by hand — the keeper entry wins
            self._set(pick_no, str(cid), keeper=True)
        return warnings

    @property
    def keeper_pick_numbers(self) -> set[int]:
        return set(self._keeper_picks)

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

    def lookup(self, raw_or_canonical_id: str) -> dict[str, object] | None:
        """One player by raw id or canonical id (``None`` if unknown)."""
        for item in self._items:
            if item.raw_id == raw_or_canonical_id or item.canonical_id == raw_or_canonical_id:
                return {"canonical_id": item.canonical_id, "raw_id": item.raw_id, "name": item.name,
                        "position": item.position, "team": item.team}
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
# Keeper entry (store-backed)
# --------------------------------------------------------------------------- #


class KeeperBook:
    """Every team's keepers, persisted in the store's ``keepers`` table.

    Entry is by *last year's round* (or undrafted); the league's
    :class:`~fantasy_coach.league.KeeperRules` derive the cost round and
    spread multiple undrafted keepers (15 → 14 → 16 → 17). Each change
    re-derives the whole team's rounds so the set is always consistent, and
    :meth:`triples` hands the result to the pick source / simulator.
    """

    def __init__(self, store, league_key: str, rules: KeeperRules | None, *, rounds: int) -> None:
        self._store = store
        self._league_key = league_key
        self.rules = rules or KeeperRules()
        self._rounds = rounds

    def rows(self) -> list[dict[str, object]]:
        return [dict(r) for r in self._store.keepers(self._league_key)]

    def by_team(self) -> dict[str, list[dict[str, object]]]:
        out: dict[str, list[dict[str, object]]] = {}
        for r in self.rows():
            out.setdefault(str(r["team_key"]), []).append(r)
        return out

    def triples(self) -> list[tuple[str, int, str]]:
        return [(str(r["team_key"]), int(r["cost_round"]), str(r["canonical_id"])) for r in self.rows()]

    def add(
        self,
        team_key: str,
        canonical_id: str,
        *,
        name: str = "",
        position: str = "",
        last_round: int | None = None,
        cost_round: int | None = None,
    ) -> list[dict[str, object]]:
        """Add (or update) a keeper; re-derives the team's cost rounds.

        ``cost_round`` overrides the rule (a commissioner exception); by
        default the rule decides from ``last_round``. Raises
        :class:`KeeperConflict` (rules) or ``ValueError`` (a player already
        kept by another team).
        """
        for r in self.rows():
            if str(r["canonical_id"]) == canonical_id and str(r["team_key"]) != team_key:
                raise ValueError(f"{r['name'] or canonical_id} is already kept by {r['team_key']}")
        team_rows = [r for r in self.by_team().get(team_key, []) if str(r["canonical_id"]) != canonical_id]
        entries = [(str(r["canonical_id"]), r["last_round"]) for r in team_rows] + [(canonical_id, last_round)]
        if len(entries) > self.rules.max_keepers:
            raise KeeperConflict(f"{team_key} would keep {len(entries)} — the rules allow {self.rules.max_keepers}")
        # Manually-fixed rounds keep their round; the rest are re-derived.
        fixed = {str(r["canonical_id"]): int(r["cost_round"]) for r in team_rows if r["source"] == "override"}
        if cost_round is not None:
            fixed[canonical_id] = int(cost_round)
        derived = dict(assign_keeper_rounds([e for e in entries if e[0] not in fixed], self.rules, rounds=self._rounds))
        rounds_used = list(fixed.values()) + list(derived.values())
        if len(set(rounds_used)) != len(rounds_used):
            raise KeeperConflict("two keepers on the team would cost the same round")
        for cid, last in entries:
            r_cost = fixed.get(cid, derived.get(cid))
            row_name, row_pos = name, position
            src = "override" if cid in fixed else "rule"
            for r in team_rows:
                if str(r["canonical_id"]) == cid:
                    row_name, row_pos = str(r["name"]), str(r["position"])
                    break
            self._store.upsert_keeper(
                self._league_key, team_key=team_key, canonical_id=cid, cost_round=int(r_cost),
                name=row_name, position=row_pos, last_round=last, source=src,
            )
        return self.by_team().get(team_key, [])

    def remove(self, team_key: str, canonical_id: str) -> int:
        n = self._store.delete_keeper(self._league_key, team_key=team_key, canonical_id=canonical_id)
        # Re-derive the survivors' rounds (an undrafted fill order may shift).
        rest = self.by_team().get(team_key, [])
        if rest and n:
            entries = [(str(r["canonical_id"]), r["last_round"]) for r in rest if r["source"] != "override"]
            for cid, rnd in assign_keeper_rounds(entries, self.rules, rounds=self._rounds):
                row = next(r for r in rest if str(r["canonical_id"]) == cid)
                self._store.upsert_keeper(
                    self._league_key, team_key=team_key, canonical_id=cid, cost_round=rnd,
                    name=str(row["name"]), position=str(row["position"]), last_round=row["last_round"],
                    source="rule",
                )
        return n

    def clear(self) -> int:
        return self._store.clear_keepers(self._league_key)


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
    keepers: KeeperBook | None = None

    # -- keepers -----------------------------------------------------------------

    def keeper_view(self) -> dict[str, object]:
        if self.keepers is None:
            return {"enabled": False, "teams": []}
        by_team = self.keepers.by_team()
        return {
            "enabled": True,
            "rules": {
                "max_keepers": self.keepers.rules.max_keepers,
                "min_draft_round_to_keep": self.keepers.rules.min_draft_round_to_keep,
                "cost_rounds_earlier": self.keepers.rules.cost_rounds_earlier,
                "undrafted_cost_round": self.keepers.rules.undrafted_cost_round,
            },
            "teams": [
                {
                    "team_key": k,
                    "name": self.team_names.get(k, k.rsplit(".", 1)[-1]),
                    "keepers": [
                        {"canonical_id": r["canonical_id"], "name": r["name"], "position": r["position"],
                         "last_round": r["last_round"], "cost_round": r["cost_round"], "source": r["source"]}
                        for r in by_team.get(k, [])
                    ],
                }
                for k in self.source.order
            ],
        }

    def add_keeper(
        self,
        team_key: str,
        raw_id: str,
        *,
        last_round: int | None,
        cost_round: int | None = None,
        off_position: str = "",
        off_name: str = "",
    ) -> dict[str, object]:
        """Record a keeper. ``raw_id`` normally names a store player; when the
        player is off-board (a rookie/DEF the store missed — rare with the
        Sleeper catalog loaded), pass ``off_position`` (+ optional
        ``off_name``) instead and the keeper is recorded under an off-board id
        that still consumes the right cost-round pick and roster slot.
        """
        if self.keepers is None:
            raise ValueError("this league has no keeper rules")
        item = self.finder.lookup(raw_id) if raw_id else None
        if item is None:
            pos = normalize_position(off_position)
            if not pos:
                raise ValueError(
                    f"unknown player {raw_id!r} — pass off_position to record "
                    "an off-board keeper"
                )
            name = (off_name or raw_id or f"Off-board {pos}").strip()
            item = {
                "canonical_id": off_board_id(pos, name),
                "name": name,
                "position": pos,
            }
        self.keepers.add(
            team_key, item["canonical_id"], name=item["name"], position=item["position"],
            last_round=last_round, cost_round=cost_round,
        )
        return self._apply_keepers(f"{item['name']} kept by {self.team_names.get(team_key, team_key)}")

    def remove_keeper(self, team_key: str, raw_id: str) -> dict[str, object]:
        if self.keepers is None:
            raise ValueError("this league has no keeper rules")
        item = self.finder.lookup(raw_id)
        cid = item["canonical_id"] if item else raw_id
        n = self.keepers.remove(team_key, cid)
        return self._apply_keepers("keeper removed" if n else "no such keeper")

    def _apply_keepers(self, message: str) -> dict[str, object]:
        warnings = self.source.apply_keepers(self.keepers.triples() if self.keepers else [])
        labels = {}
        if self.keepers is not None:
            for r in self.keepers.rows():
                labels[str(r["canonical_id"])] = f"keeper · Rd {r['cost_round']}"
        set_labels = getattr(self.loop, "set_keeper_labels", None)
        if callable(set_labels):
            set_labels(labels)
        out = self._after(message + ("; " + "; ".join(warnings) if warnings else ""))
        out["keepers"] = self.keeper_view()
        return out

    def search(self, query: str, *, limit: int = 8, position: str = "") -> list[dict[str, object]]:
        return self.finder.search(
            query, taken=self.source.picks_by_player(), limit=limit, position=position
        )

    def mark(self, raw_id: str, *, team_key: str = "", pick_no: int | None = None) -> dict[str, object]:
        pick = self.source.mark(raw_id, team_key=team_key or None, pick_no=pick_no)
        return self._after(f"marked pick {pick.pick} ({self.team_names.get(pick.team_key, pick.team_key)})")

    def mark_unknown(
        self,
        position: str,
        *,
        name: str = "",
        team_key: str = "",
        pick_no: int | None = None,
    ) -> dict[str, object]:
        """Record an **off-board pick** — a player the store doesn't know
        (P0-2c: a deep rookie or obscure DEF another manager just drafted).

        The pick consumes the slot and a roster spot at ``position`` (the
        off-board id encodes it), so whose-turn / picks-until-next / survival
        all stay correct and the loop never stalls waiting for a player the
        finder can't find. With the Sleeper catalog loaded this is rare — but
        the draft clock doesn't care about rare.
        """
        pos = normalize_position(position)
        if not pos:
            raise ValueError("an off-board pick needs a position (QB/RB/…/DEF)")
        raw = off_board_id(pos, name)
        # Two anonymous off-board LBs must be two picks, not one moved pick —
        # suffix a counter until the id is unique among made picks.
        taken = self.source.picks_by_player()
        base, n = raw, 2
        while raw in taken:
            raw = f"{base} #{n}"
            n += 1
        pick = self.source.mark(raw, team_key=team_key or None, pick_no=pick_no)
        who = self.team_names.get(pick.team_key, pick.team_key)
        label = name.strip() or f"off-board {pos}"
        return self._after(f"marked pick {pick.pick} ({who}) — {label}")

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
