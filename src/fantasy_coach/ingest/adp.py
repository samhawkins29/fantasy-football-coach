"""Free market ADP from FantasyFootballCalculator (P0-2 / P1-1).

The board's whole market layer — the ADP→VORP gap-fill for rookies/DEFs, the
consensus blend's market signal, ADP-anchored survival — was starved because
the only ADP source (Yahoo ``draft_analysis``) is gated behind app approval.
FantasyFootballCalculator publishes real mock-draft ADP through a **free,
keyless** JSON API::

    https://fantasyfootballcalculator.com/api/v1/adp/ppr?teams=10&year=2026

scored per format (``ppr``/``half-ppr``/``standard``) and league size, with
per-player stdev and bye — exactly the shape ``estimate_survival`` and the
``adp`` table want. This module fetches it, caches it per season (draft day is
zero-network, framework §7), and resolves its names onto the store's canonical
players (clean-name + position; defenses by team; ``PK`` → ``K``).

ADP is market data, honestly labelled: rows land in the ``adp`` table under
``source="ffc"`` and are consumed exactly like Yahoo's — nothing downstream
changes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fantasy_coach.ingest.names import clean_name, normalize_position, normalize_team

__all__ = ["FFC_ADP_URL", "AdpRecord", "FfcAdpSource", "resolve_adp"]

logger = logging.getLogger(__name__)

#: The keyless ADP endpoint. ``format`` is ``ppr`` / ``half-ppr`` / ``standard``
#: (pick the one matching the league's scoring), ``teams`` the mock-room size.
FFC_ADP_URL = (
    "https://fantasyfootballcalculator.com/api/v1/adp/{format}"
    "?teams={teams}&year={year}"
)


@dataclass(slots=True)
class AdpRecord:
    """One player's market ADP as FFC reports it (source-native naming)."""

    name: str
    position: str  # normalized (PK → K, DEF stays DEF)
    team: str
    adp: float
    stdev: float | None = None
    high: float | None = None
    low: float | None = None
    bye: int | None = None
    times_drafted: int = 0


def _records_from_payload(payload: Mapping[str, object]) -> list[AdpRecord]:
    out: list[AdpRecord] = []
    for row in payload.get("players", []) or []:
        if not isinstance(row, Mapping):
            continue
        try:
            adp = float(row["adp"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        position = normalize_position(str(row.get("position") or ""))
        if position == "PK":
            position = "K"
        out.append(
            AdpRecord(
                name=str(row.get("name", "")),
                position=position,
                team=normalize_team(str(row.get("team") or "")),
                adp=adp,
                stdev=(None if row.get("stdev") is None else float(row["stdev"])),  # type: ignore[arg-type]
                high=(None if row.get("high") is None else float(row["high"])),  # type: ignore[arg-type]
                low=(None if row.get("low") is None else float(row["low"])),  # type: ignore[arg-type]
                bye=(int(row["bye"]) if isinstance(row.get("bye"), int) else None),
                times_drafted=int(row.get("times_drafted", 0) or 0),
            )
        )
    return out


@dataclass(slots=True)
class FfcAdpSource:
    """Cache-first FantasyFootballCalculator ADP (free, keyless).

    Args:
        scoring_format: ``"ppr"`` / ``"half-ppr"`` / ``"standard"`` — match
            the league's reception scoring.
        teams: The mock-room size (match the league's team count — ADP in a
            10-team room differs from a 12-team one).
        cache_dir: Per-season JSON cache location (git-ignored).
        client: Injectable ``httpx.Client`` (tests use ``MockTransport``).
    """

    name: str = "ffc"
    scoring_format: str = "ppr"
    teams: int = 12
    cache_dir: Path = field(default_factory=lambda: Path(".cache"))
    client: object | None = field(default=None, repr=False)
    timeout: float = 20.0

    @property
    def is_live(self) -> bool:
        """True — the API needs no key."""
        return True

    def _cache_path(self, season: int) -> Path:
        fmt = self.scoring_format.replace("-", "")
        return self.cache_dir / f"adp_ffc_{fmt}_{self.teams}_{season}.json"

    def _get_client(self):
        if self.client is None:
            import httpx  # noqa: PLC0415  (lazy — tests inject)

            self.client = httpx.Client(timeout=self.timeout)
        return self.client

    def warm_cache(self, season: int) -> list[AdpRecord]:
        """Pull live ADP for ``season``, persist the cache, return records."""
        url = FFC_ADP_URL.format(
            format=self.scoring_format, teams=self.teams, year=season
        )
        resp = self._get_client().get(url)
        resp.raise_for_status()
        payload = resp.json()
        records = _records_from_payload(payload if isinstance(payload, dict) else {})
        if not records:
            raise RuntimeError(f"FFC ADP returned no players for {season} ({url})")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(season).write_text(
            json.dumps(
                {
                    "source": self.name,
                    "meta": (payload.get("meta") if isinstance(payload, dict) else {}),
                    "players": [
                        {
                            "name": r.name,
                            "position": r.position,
                            "team": r.team,
                            "adp": r.adp,
                            "stdev": r.stdev,
                            "high": r.high,
                            "low": r.low,
                            "bye": r.bye,
                            "times_drafted": r.times_drafted,
                        }
                        for r in records
                    ],
                }
            ),
            encoding="utf-8",
        )
        return records

    def load(self, season: int) -> list[AdpRecord]:
        """The cached ADP with zero network. Raises if never warmed."""
        path = self._cache_path(season)
        if not path.exists():
            raise RuntimeError(
                f"no FFC ADP cache at {path} — run warm_cache() while online"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _records_from_payload(payload)


def resolve_adp(
    records: Iterable[AdpRecord],
    players: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    """Join ADP records onto store player rows → ``upsert_adp`` rows + warnings.

    ``players`` are store rows (``canonical_id``/``name``/``position``/
    ``team``). Matching: defenses by team code; everyone else by clean name +
    position, with the team code breaking name ties. Unmatched records are
    returned as warnings, never silently dropped (§3.2 honesty).
    """
    players = [dict(p) for p in players]  # sqlite3.Row → plain mapping
    by_key: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    def_by_team: dict[str, Mapping[str, object]] = {}
    for p in players:
        pos = normalize_position(str(p.get("position", "") or ""))
        if pos == "DEF":
            team = normalize_team(str(p.get("team", "") or ""))
            if team:
                def_by_team.setdefault(team, p)
            continue
        key = (clean_name(str(p.get("name", "") or "")), pos)
        by_key.setdefault(key, []).append(p)

    rows: list[dict[str, object]] = []
    warnings: list[str] = []
    for rec in records:
        if rec.position == "DEF":
            row = def_by_team.get(rec.team)
            if row is None:
                warnings.append(f"no DEF row for team {rec.team!r} ({rec.name})")
                continue
        else:
            cands = by_key.get((clean_name(rec.name), rec.position), [])
            if len(cands) > 1 and rec.team:
                narrowed = [
                    c
                    for c in cands
                    if normalize_team(str(c.get("team", "") or "")) == rec.team
                ]
                cands = narrowed or cands
            if not cands:
                warnings.append(
                    f"ADP name {rec.name!r} ({rec.position} {rec.team}) not in players"
                )
                continue
            if len(cands) > 1:
                warnings.append(
                    f"ADP name {rec.name!r} ({rec.position}) ambiguous — skipped"
                )
                continue
            row = cands[0]
        rows.append(
            {
                "canonical_id": str(row["canonical_id"]),
                "average_pick": rec.adp,
                "stdev": rec.stdev,
            }
        )
    return rows, warnings
