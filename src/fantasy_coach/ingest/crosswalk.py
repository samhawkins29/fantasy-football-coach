"""The master id crosswalk — hub-and-spoke id table (framework §3.1, §3.2 step 1).

This module owns the *data* half of the crosswalk: one row per real player,
carrying that player's id in every provider's namespace, plus the normalized
identity (``clean_name``, ``position``, ``team``) the fallback stages match on.
The *matching logic* lives in :mod:`fantasy_coach.ingest.resolver`; this module
just loads, normalizes, and indexes the table so the resolver's lookups are O(1)
(direct id / deterministic tuple) or bucketed (fuzzy).

Two loaders, matching §3.2 steps 1:

* :func:`load_id_crosswalk` — from ``nfl_data_py.import_ids()`` (the
  DynastyProcess ``db_playerids`` table), the primary source. The fetch function
  is **injectable** so tests never hit the network and so a live pull is opt-in.
* :meth:`IdCrosswalk.gap_fill_from_sleeper` — merge Sleeper's ``yahoo_id`` (and
  other ids) in where DynastyProcess lacks a mapping (§3.1 "second yahoo
  source"). Sleeper only *fills gaps*; it never overwrites a primary mapping.

The important real-world quirks this handles (all observed in a live
``import_ids()`` pull):

* ids arrive as **floats** (``yahoo_id`` ``32692.0``) with ``NaN`` for missing —
  coerced to clean strings, ``NaN`` → ``None``;
* **duplicate names** across different players (two "Justin Jefferson", a WR and
  an old LB) — never keyed on name alone; the deterministic index keys on the
  full ``(clean_name, position, team)`` tuple and records collisions;
* **~56% of rows have no ``yahoo_id``** — so the crosswalk is a pipeline of
  fallbacks, not a single lookup.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from fantasy_coach.ingest.names import MatchKey, clean_name, normalize_position, normalize_team

__all__ = [
    "CrosswalkRow",
    "IdCrosswalk",
    "clean_source_id",
    "load_id_crosswalk",
    "ID_MAP_COLUMNS",
]

# The columns we lift out of the DynastyProcess id map, mapped from that table's
# column name -> our :class:`~fantasy_coach.ingest.canonical.ExternalIds` field.
# (import_ids() carries ~35 columns; these are the ones the app joins on.)
ID_MAP_COLUMNS = {
    "gsis_id": "gsis_id",
    "yahoo_id": "yahoo_id",
    "sleeper_id": "sleeper_id",
    "espn_id": "espn_id",
    "pfr_id": "pfr_id",
    "fantasypros_id": "fantasypros_id",
    "rotowire_id": "rotowire_id",
    "sportradar_id": "sportradar_id",
    "mfl_id": "mfl_id",
    "pff_id": "pff_id",
    "cbs_id": "cbs_id",
}


def clean_source_id(value: object) -> str | None:
    """Coerce a raw id cell into a clean string, or ``None`` if absent.

    Handles the shapes ``import_ids()`` actually returns:

    * ``NaN`` (float) / ``None`` / empty  -> ``None``
    * a whole-number float ``32692.0``    -> ``"32692"`` (drops the ``.0``)
    * an int ``32692``                    -> ``"32692"``
    * a string ``"00-0036322"``           -> unchanged (gsis ids are strings)

    The ``.0``-stripping matters: a Yahoo id compared as ``"32692"`` must equal
    the crosswalk's ``32692.0`` cell, or every direct join would miss.
    """
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return None
    # A stringified whole-number float ("32692.0") -> "32692".
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


@dataclass(slots=True)
class CrosswalkRow:
    """One player's row in the master crosswalk.

    ``ids`` maps our :class:`ExternalIds` field names to cleaned string ids (only
    the ones present for this player). ``gsis_id`` is pulled out for the hub key.
    ``clean_name``/``position``/``team`` are pre-normalized so the resolver never
    re-normalizes a crosswalk row on the hot path.
    """

    gsis_id: str | None
    name: str
    clean_name: str
    position: str
    team: str
    ids: dict[str, str] = field(default_factory=dict)

    @property
    def match_key(self) -> MatchKey:
        """The ``(clean_name, position, team)`` tuple the deterministic stage uses."""
        return (self.clean_name, self.position, self.team)

    def yahoo_id(self) -> str | None:
        """This player's Yahoo id, if the crosswalk has one."""
        return self.ids.get("yahoo_id")


class IdCrosswalk:
    """An indexed, hub-and-spoke player id table (framework §3.1).

    Build it via :func:`load_id_crosswalk` (from ``import_ids()``) or directly
    from :class:`CrosswalkRow` objects, then hand it to an
    :class:`~fantasy_coach.ingest.resolver.IdResolver`. It exposes exactly the
    lookups the resolver's stages need:

    * :meth:`by_yahoo_id` / :meth:`by_source_id` — the happy-path direct joins.
    * :meth:`by_match_key` — the deterministic ``(name, pos, team)`` join;
      returns ``None`` when a key is ambiguous (a name collision), so the
      resolver can fall through to fuzzy rather than pick wrong.
    * :meth:`candidates_in_bucket` — the ``(position, team)`` shortlist the
      fuzzy stage scores against.
    """

    def __init__(self, rows: Iterable[CrosswalkRow]) -> None:
        self._rows: list[CrosswalkRow] = list(rows)

        # Direct id indices, one per provider namespace (yahoo_id, sleeper_id…).
        self._by_id: dict[str, dict[str, CrosswalkRow]] = defaultdict(dict)
        # Deterministic tuple index; value is a *list* so we can detect collisions.
        self._by_key: dict[MatchKey, list[CrosswalkRow]] = defaultdict(list)
        # Fuzzy buckets keyed by (position, team).
        self._buckets: dict[tuple[str, str], list[CrosswalkRow]] = defaultdict(list)

        for row in self._rows:
            for field_name, value in row.ids.items():
                if value is not None:
                    # First writer wins on a given id (ids should be unique).
                    self._by_id[field_name].setdefault(value, row)
            self._by_key[row.match_key].append(row)
            self._buckets[(row.position, row.team)].append(row)

    # -- construction helpers ----------------------------------------------

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def rows(self) -> list[CrosswalkRow]:
        """Every row in the crosswalk (read-only view)."""
        return list(self._rows)

    # -- direct id lookups (happy path, §3.2 step 2) ------------------------

    def by_source_id(self, field_name: str, source_id: str | None) -> CrosswalkRow | None:
        """Return the row whose ``field_name`` id equals ``source_id``."""
        if not source_id:
            return None
        return self._by_id.get(field_name, {}).get(source_id)

    def by_yahoo_id(self, yahoo_id: str | None) -> CrosswalkRow | None:
        """Return the row for a Yahoo player id (the primary direct join)."""
        return self.by_source_id("yahoo_id", yahoo_id)

    # -- deterministic tuple lookup (§3.2 step 3) ---------------------------

    def by_match_key(self, key: MatchKey) -> CrosswalkRow | None:
        """Return the unique row for a ``(clean_name, pos, team)`` key.

        Returns ``None`` if no row matches **or** if more than one does — an
        ambiguous key (a genuine name collision, or missing team on both sides)
        must not silently resolve to whichever row happened to be first. The
        resolver falls through to the fuzzy stage in that case.
        """
        matches = self._by_key.get(key)
        if not matches or len(matches) != 1:
            return None
        return matches[0]

    def collisions(self) -> dict[MatchKey, list[CrosswalkRow]]:
        """Every ``(name, pos, team)`` key that maps to more than one player.

        Useful for a data-quality report — most collisions are missing-team rows
        (``team == ""``); a collision *with* a team is worth curating an override.
        """
        return {key: rows for key, rows in self._by_key.items() if len(rows) > 1}

    # -- fuzzy bucket (§3.2 step 4) -----------------------------------------

    def candidates_in_bucket(self, position: str, team: str) -> list[CrosswalkRow]:
        """Rows sharing a ``(position, team)`` — the fuzzy stage's shortlist.

        Restricting fuzzy matching to same-position, same-team candidates keeps
        it fast and dramatically cuts false positives (you can't fuzzily match a
        WR onto a QB, or a Chiefs player onto a Jets player).
        """
        return list(self._buckets.get((position, team), ()))

    def candidates_by_team(self, team: str) -> list[CrosswalkRow]:
        """Every row on a team, across positions (looser fuzzy fallback)."""
        out: list[CrosswalkRow] = []
        for (_, bucket_team), rows in self._buckets.items():
            if bucket_team == team:
                out.extend(rows)
        return out

    # -- gap-fill (§3.1 "second yahoo source") ------------------------------

    def gap_fill_from_sleeper(
        self, sleeper_players: Mapping[str, Mapping[str, object]]
    ) -> int:
        """Fill missing ids from Sleeper's ``players/nfl`` blob; return #filled.

        Sleeper's player objects carry ``yahoo_id``, ``gsis_id``, ``espn_id``,
        ``rotowire_id`` etc. We match a Sleeper player to a crosswalk row by
        ``sleeper_id`` (the dict key) and fill *only* ids the row is missing —
        DynastyProcess remains authoritative (§3.1). Rows are re-indexed after,
        so a Yahoo id filled from Sleeper is immediately joinable.

        Args:
            sleeper_players: ``{sleeper_id: {"yahoo_id": ..., "gsis_id": ...}}``
                — the shape of ``GET /v1/players/nfl``.

        Returns:
            The number of individual id cells filled.
        """
        by_sleeper = self._by_id.get("sleeper_id", {})
        # Also allow matching Sleeper->row by gsis when sleeper_id is absent.
        by_gsis = {r.gsis_id: r for r in self._rows if r.gsis_id}
        filled = 0
        newly_indexed: list[tuple[str, str, CrosswalkRow]] = []

        for sleeper_id, obj in sleeper_players.items():
            row = by_sleeper.get(str(sleeper_id))
            if row is None:
                gsis = clean_source_id(obj.get("gsis_id"))
                row = by_gsis.get(gsis) if gsis else None
            if row is None:
                continue
            # Ensure the sleeper_id itself is recorded on the row.
            if row.ids.get("sleeper_id") is None:
                row.ids["sleeper_id"] = str(sleeper_id)
                newly_indexed.append(("sleeper_id", str(sleeper_id), row))
                filled += 1
            for our_field in ID_MAP_COLUMNS:
                if row.ids.get(our_field) is not None:
                    continue
                value = clean_source_id(obj.get(our_field))
                if value is not None:
                    row.ids[our_field] = value
                    newly_indexed.append((our_field, value, row))
                    filled += 1

        # Re-index the newly filled ids so direct joins see them.
        for field_name, value, row in newly_indexed:
            self._by_id[field_name].setdefault(value, row)
        return filled


def load_id_crosswalk(
    fetch: Callable[..., object] | None = None,
    *,
    rows: Sequence[Mapping[str, object]] | None = None,
    columns: Mapping[str, str] = ID_MAP_COLUMNS,
) -> IdCrosswalk:
    """Build an :class:`IdCrosswalk` from the DynastyProcess id map (§3.2 step 1).

    Offline-first: pass ``rows`` (a list of dict-like records) to build from a
    fixture with no dependency on ``nfl_data_py`` or the network. In production,
    pass ``fetch=nfl_data_py.import_ids`` (or leave it to be imported lazily) and
    the function reads the columns it needs off the returned DataFrame.

    Args:
        fetch: A callable returning the id map — a ``pandas.DataFrame`` (as
            ``import_ids()`` does) or any object with ``.to_dict("records")``.
            Imported lazily from ``nfl_data_py`` if neither ``fetch`` nor
            ``rows`` is given, so importing this module never imports pandas.
        rows: Pre-materialized records (skips ``fetch`` entirely). Each is a
            mapping with at least ``name``/``position``/``team`` and any id
            columns. This is the offline test path.
        columns: Map of source-column -> :class:`ExternalIds` field to lift.

    Returns:
        A fully-indexed :class:`IdCrosswalk`.
    """
    if rows is None:
        if fetch is None:
            # Lazy import keeps pandas/nfl_data_py out of import time; only the
            # live path pays for it (tests always pass ``rows``).
            import nfl_data_py  # noqa: PLC0415  (intentional lazy import)

            fetch = nfl_data_py.import_ids
        result = fetch()
        rows = _to_records(result)

    crosswalk_rows = [_row_from_record(rec, columns) for rec in rows]
    return IdCrosswalk(crosswalk_rows)


def _to_records(result: object) -> list[Mapping[str, object]]:
    """Coerce a fetch result (DataFrame or list) into a list of dict records."""
    if hasattr(result, "to_dict"):
        # pandas.DataFrame -> list of row dicts.
        return result.to_dict("records")  # type: ignore[no-any-return, call-arg]
    if isinstance(result, Sequence):
        return list(result)  # type: ignore[return-value]
    raise TypeError(
        f"Unsupported id-map fetch result type: {type(result)!r} "
        "(expected a pandas.DataFrame or a sequence of records)"
    )


def _row_from_record(
    record: Mapping[str, object], columns: Mapping[str, str]
) -> CrosswalkRow:
    """Turn one id-map record into a normalized :class:`CrosswalkRow`."""
    ids: dict[str, str] = {}
    for source_col, our_field in columns.items():
        value = clean_source_id(record.get(source_col))
        if value is not None:
            ids[our_field] = value

    name = str(record.get("name") or record.get("full_name") or "").strip()
    position = normalize_position(str(record.get("position") or ""))
    team = normalize_team(str(record.get("team") or ""))
    # Always normalize with *our* clean_name so the row's key and a Yahoo
    # identity's key (both via match_key/clean_name) use the identical transform.
    # nflverse's own ``merge_name`` normalizes differently (it keeps hyphens:
    # "amon-ra st brown" vs our "amon ra st brown"), so mixing the two would make
    # the deterministic (name, pos, team) join miss on hyphenated/punctuated
    # names and fall through to fuzzy. We deliberately ignore merge_name here.
    cleaned = clean_name(name)

    return CrosswalkRow(
        gsis_id=ids.get("gsis_id"),
        name=name,
        clean_name=cleaned,
        position=position,
        team=team,
        ids=ids,
    )
