"""The league spec file: your league's exact rules, offline (``data/league.json``).

Yahoo's live API is what tells the Coach a league's scoring, roster and
playoff shape — but that path is gated behind Yahoo's app review, and the
board must model *your* league exactly regardless (roster demand sets every
replacement baseline; the scoring type sets every point). So the rules can
also come from a small JSON file the founder writes once. Everything the
value engine needs is expressible: teams, scoring per stat, the full lineup
(including IDP and no-kicker rosters), playoff weeks, draft length, and the
keeper mechanics of a keeper league.

The file shape (see ``data/league.json`` for the founder's real league)::

    {
      "league_key": "sam.l.2026",            # any key; the store keys on it
      "name": "…", "num_teams": 10,
      "scoring": {"rec": 1.0, "pass_td": 4, …},   # projection stat keys → pts
      "roster": [{"position": "QB", "count": 1}, {"position": "W/R/T", "count": 2},
                 {"position": "D", "count": 1}, {"position": "BN", "count": 8}],
      "regular_season_weeks": 14,
      "playoffs": {"start_week": 15, "num_teams": 6, "byes": 2},
      "draft": {"rounds": 17, "type": "snake", "date": "2026-09-04T19:15-04:00",
                "my_slot": null},
      "keeper_rules": {"max_keepers": 4, "min_draft_round_to_keep": 4,
                       "cost_rounds_earlier": 3, "undrafted_cost_round": 15},
      "keepers": {"3": [{"player": "Puka Nacua", "round": 6}], …}   # slot → keepers
    }

``scoring`` keys are the projection stat keys (``pass_yds``/``rec``/
``tackle_solo``…); they are mapped back onto Yahoo stat ids so the resulting
:class:`~fantasy_coach.clients.models.LeagueSettings` is indistinguishable
from a live one downstream. Roster codes are Yahoo's (``W/R/T`` flex,
``D`` = any IDP, ``DL``/``LB``/``DB``, ``DEF``, ``BN``, ``IR``).

**Keepers.** Kept players are removed from the draft pool and *consume the
keeping team's pick in the cost round* (Yahoo pre-populates those picks as
made; the simulator scripts them the same way). ``keepers`` maps a team slot
(round-1 draft position, ``"1"``…``"10"``) or a full team key to its kept
players with the round each one costs — the founder computes the round from
the rules (last year's round − 3, undrafted → 15) and enters it. Names are
resolved against the store's player table (exact clean-name match, position
disambiguates); unresolved keepers are reported, never silently dropped.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fantasy_coach.clients.models import LeagueSettings, RosterPosition, StatCategory
from fantasy_coach.ingest.names import clean_name, normalize_position
from fantasy_coach.value.scoring import YAHOO_STAT_KEYS

__all__ = [
    "KeeperRules",
    "Keeper",
    "ResolvedKeeper",
    "LeagueSpec",
    "load_league_spec",
    "team_key_for_slot",
    "resolve_keepers",
    "keeper_note",
]

_STAT_ID_BY_KEY = {key: sid for sid, key in YAHOO_STAT_KEYS.items()}


@dataclass(slots=True)
class KeeperRules:
    """The keeper mechanics that matter to the draft.

    ``min_draft_round_to_keep``: players drafted in earlier rounds can't be
    kept next year (the founder's league: rounds 1–3 are un-keepable);
    ``cost_rounds_earlier``: a keeper costs a pick this many rounds earlier
    than the round he was drafted in; ``undrafted_cost_round``: what an
    undrafted (waiver) keeper costs.
    """

    max_keepers: int = 4
    min_draft_round_to_keep: int = 4
    cost_rounds_earlier: int = 3
    undrafted_cost_round: int = 15


@dataclass(slots=True)
class Keeper:
    """One kept player as written in the spec (unresolved)."""

    team: str  # slot number ("3") or full team key
    player: str  # name or canonical id
    round: int
    position: str = ""


@dataclass(slots=True)
class ResolvedKeeper:
    """A keeper joined to the store: who, which team key, which pick round."""

    team_key: str
    round: int
    canonical_id: str
    name: str
    position: str


@dataclass(slots=True)
class LeagueSpec:
    """The parsed spec: settings for the value engine + the draft-side extras."""

    settings: LeagueSettings
    name: str = ""
    rounds: int = 15
    regular_season_weeks: int | None = None
    playoff_byes: int = 0
    draft_type: str = "snake"
    draft_date: str = ""
    my_slot: int | None = None
    keeper_rules: KeeperRules | None = None
    keepers: list[Keeper] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    path: Path | None = None

    @property
    def league_key(self) -> str:
        return self.settings.league_key

    @property
    def num_teams(self) -> int:
        return self.settings.max_teams or 10

    @property
    def is_keeper_league(self) -> bool:
        return self.keeper_rules is not None


def team_key_for_slot(league_key: str, slot: int) -> str:
    """The team key the simulator/store use for round-1 slot ``slot``."""
    return f"{league_key}.t.{slot}"


def _settings_from(payload: Mapping[str, object]) -> LeagueSettings:
    scoring = payload.get("scoring") or {}
    unknown = [k for k in scoring if k not in _STAT_ID_BY_KEY]
    if unknown:
        raise ValueError(
            f"league spec scoring has unknown stat keys {unknown}; known: "
            f"{sorted(_STAT_ID_BY_KEY)}"
        )
    cats = [
        StatCategory(stat_id=_STAT_ID_BY_KEY[key], value=float(val))
        for key, val in scoring.items()
    ]
    roster = []
    for slot in payload.get("roster") or []:
        pos = str(slot["position"]).strip()
        count = int(slot.get("count", 1))
        starting = bool(slot.get("is_starting_position", pos not in ("BN", "IR", "IL", "IR+")))
        roster.append(RosterPosition(position=pos, count=count, is_starting_position=starting))
    playoffs = payload.get("playoffs") or {}
    return LeagueSettings(
        league_key=str(payload.get("league_key") or "offline.l.1"),
        scoring_type=str(payload.get("scoring_type") or "head"),
        draft_type=str((payload.get("draft") or {}).get("type") or "live"),
        uses_playoff=bool(playoffs),
        playoff_start_week=int(playoffs["start_week"]) if "start_week" in playoffs else None,
        num_playoff_teams=int(playoffs["num_teams"]) if "num_teams" in playoffs else None,
        max_teams=int(payload["num_teams"]) if "num_teams" in payload else None,
        roster_positions=roster,
        stat_categories=cats,
    )


def load_league_spec(path: str | Path) -> LeagueSpec:
    """Parse a league spec file into settings + draft extras (raises on a bad file)."""
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    settings = _settings_from(payload)
    draft = payload.get("draft") or {}
    rules_raw = payload.get("keeper_rules")
    rules = (
        KeeperRules(**{k: int(v) for k, v in dict(rules_raw).items()})
        if rules_raw is not None
        else None
    )
    keepers: list[Keeper] = []
    for team, kept in dict(payload.get("keepers") or {}).items():
        for k in kept or []:
            keepers.append(
                Keeper(
                    team=str(team),
                    player=str(k["player"]),
                    round=int(k["round"]),
                    position=normalize_position(str(k.get("position", "") or "")),
                )
            )
    playoffs = payload.get("playoffs") or {}
    return LeagueSpec(
        settings=settings,
        name=str(payload.get("name") or ""),
        rounds=int(draft.get("rounds") or 15),
        regular_season_weeks=(
            int(payload["regular_season_weeks"]) if "regular_season_weeks" in payload else None
        ),
        playoff_byes=int(playoffs.get("byes", 0) or 0),
        draft_type=str(draft.get("type") or "snake"),
        draft_date=str(draft.get("date") or ""),
        my_slot=int(draft["my_slot"]) if draft.get("my_slot") is not None else None,
        keeper_rules=rules,
        keepers=keepers,
        notes=[str(n) for n in payload.get("notes") or []],
        path=path,
    )


def resolve_keepers(
    spec: LeagueSpec, players: Sequence[Mapping[str, object]]
) -> tuple[list[ResolvedKeeper], list[str]]:
    """Join the spec's keepers to store player rows (``canonical_id``/``name``/``position``).

    A keeper written as a canonical id matches directly; a name matches on
    the cleaned name (position narrows ties). Returns the resolved list plus
    warnings for anything unresolved or ambiguous.
    """
    players = [dict(p) for p in players]  # sqlite rows → plain mappings
    by_id = {str(p["canonical_id"]): p for p in players}
    by_clean: dict[str, list[Mapping[str, object]]] = {}
    for p in players:
        by_clean.setdefault(clean_name(str(p.get("name", ""))), []).append(p)

    resolved: list[ResolvedKeeper] = []
    warnings: list[str] = []
    for k in spec.keepers:
        team_key = (
            k.team if "." in k.team else team_key_for_slot(spec.league_key, int(k.team))
        )
        row = by_id.get(k.player)
        if row is None:
            cands = by_clean.get(clean_name(k.player), [])
            if k.position:
                cands = [c for c in cands if normalize_position(str(c.get("position", ""))) == k.position]
            if len(cands) == 1:
                row = cands[0]
            elif len(cands) > 1:
                warnings.append(
                    f"keeper {k.player!r} is ambiguous ({len(cands)} matches) — add \"position\""
                )
                continue
        if row is None:
            warnings.append(f"keeper {k.player!r} ({k.team}, round {k.round}) not found in the store")
            continue
        resolved.append(
            ResolvedKeeper(
                team_key=team_key,
                round=k.round,
                canonical_id=str(row["canonical_id"]),
                name=str(row.get("name", k.player)),
                position=normalize_position(str(row.get("position", ""))),
            )
        )
    if spec.keeper_rules is not None:
        per_team: dict[str, int] = {}
        for r in resolved:
            per_team[r.team_key] = per_team.get(r.team_key, 0) + 1
        for team_key, n in per_team.items():
            if n > spec.keeper_rules.max_keepers:
                warnings.append(
                    f"{team_key} keeps {n} players — the rules allow {spec.keeper_rules.max_keepers}"
                )
    return resolved, warnings


def keeper_note(round_no: int, rules: KeeperRules | None) -> str:
    """One line on next year's keeper value of a pick made in ``round_no``."""
    if rules is None or round_no <= 0:
        return ""
    if round_no < rules.min_draft_round_to_keep:
        return f"Round {round_no} pick — not keeper-eligible next year"
    cost = max(1, round_no - rules.cost_rounds_earlier)
    return f"Keepable next year at a round-{cost} cost (drafted round {round_no})"
