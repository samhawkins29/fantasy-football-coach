"""Yahoo Fantasy JSON normalization layer (M2).

Yahoo's ``?format=json`` output is famously awkward (framework §2.3):

* **Collections are objects, not arrays.** A list of N things is encoded as
  ``{"0": {...}, "1": {...}, ..., "count": N}`` — numeric *string* keys plus a
  sibling ``count``. :func:`iter_collection` turns that back into an iterable.
* **Object fields are positional.** A single entity (player, team) is often a
  *list of one-key dicts* — ``[{"player_key": ...}, {"player_id": ...},
  {"name": {...}}, ...]`` — and sometimes that list is itself wrapped in another
  list alongside sub-resource dicts: ``[[<kv list>], {"percent_owned": [...]}]``.
  :func:`collapse` flattens any of these shapes into one flat dict.
* **Everything is a string.** ``num_teams`` is ``"12"``, flags are ``"1"``/``"0"``.
  The ``_int`` / ``_float`` / ``_bool`` coercers handle that (and missing keys).

The rule (framework §2.3): *flatten this once here* so no other module ever
sees the positional/count quirks. Every ``parse_*`` returns clean models from
:mod:`fantasy_coach.clients.models`, each retaining the flattened dict in
``raw`` for debugging.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from fantasy_coach.clients.keys import game_key_of
from fantasy_coach.clients.models import (
    BENCH_POSITIONS,
    DraftAnalysis,
    DraftPick,
    Game,
    League,
    LeagueSettings,
    Manager,
    Matchup,
    MatchupTeam,
    Player,
    PlayerRank,
    RawDict,
    RosterPosition,
    RosterSlot,
    StatCategory,
    Team,
    TeamRoster,
    Transaction,
    TransactionPlayer,
)


class YahooParseError(ValueError):
    """Raised when a payload does not have the shape a parser expects."""


# --------------------------------------------------------------------------- #
# Primitive coercion — Yahoo sends strings for everything.
# --------------------------------------------------------------------------- #


def _int(value: object, default: int | None = None) -> int | None:
    """Coerce Yahoo's stringy numbers to ``int`` (``default`` on empty/bad)."""
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float | None = None) -> float | None:
    """Coerce to ``float`` (``default`` on empty/bad). Handles ``"12.5"``, ``"-"``."""
    if value is None or value == "" or value == "-":
        return default
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bool(value: object, default: bool = False) -> bool:
    """Coerce Yahoo's ``"1"``/``"0"`` (and real bools) to ``bool``."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip() not in ("0", "false", "False", "")


def _str(value: object, default: str = "") -> str:
    """Coerce to a stripped ``str`` (``default`` when missing/None)."""
    if value is None:
        return default
    return str(value).strip()


# --------------------------------------------------------------------------- #
# Structural helpers — the whole point of this module.
# --------------------------------------------------------------------------- #


def unwrap(payload: Mapping) -> RawDict:
    """Return the ``fantasy_content`` envelope every Yahoo response is wrapped in.

    Raises:
        YahooParseError: If ``fantasy_content`` is absent.
    """
    if not isinstance(payload, Mapping) or "fantasy_content" not in payload:
        raise YahooParseError("payload has no 'fantasy_content' envelope")
    content = payload["fantasy_content"]
    if not isinstance(content, Mapping):
        raise YahooParseError("'fantasy_content' is not an object")
    return dict(content)


def iter_collection(node: object) -> Iterator[object]:
    """Yield the members of a Yahoo count/index collection, in index order.

    Handles ``{"0": ..., "1": ..., "count": N}`` (the common case) by walking
    the numeric-string keys in ascending integer order. The ``count`` key (and
    any other non-numeric sibling such as ``coverage_type``/``week``) is skipped
    automatically, so this is robust even when ``count`` is missing or wrong.

    A plain ``list`` is yielded as-is. Anything else yields nothing.
    """
    if isinstance(node, list):
        yield from node
        return
    if not isinstance(node, Mapping):
        return
    numeric_keys = sorted(
        (k for k in node if isinstance(k, str) and k.isdigit()), key=int
    )
    for key in numeric_keys:
        yield node[key]


def collection_count(node: object) -> int:
    """Return a collection's ``count`` (falling back to the number of members)."""
    if isinstance(node, Mapping) and "count" in node:
        parsed = _int(node["count"])
        if parsed is not None:
            return parsed
    return sum(1 for _ in iter_collection(node))


def collapse(node: object) -> RawDict:
    """Flatten Yahoo's positional entity encodings into one flat dict.

    Accepts any of:

    * a plain dict — returned as a shallow copy;
    * a list of one-key dicts — ``[{"a": 1}, {"b": 2}]`` → ``{"a": 1, "b": 2}``;
    * a nested list — ``[[{"a": 1}], {"sub": ...}]`` → ``{"a": 1, "sub": ...}``
      (the leading ``[<kv list>]`` metadata block plus trailing sub-resources).

    Later keys win over earlier ones (sub-resources override metadata on clash,
    which never happens in practice but keeps the merge well-defined).
    """
    merged: RawDict = {}
    if isinstance(node, Mapping):
        return dict(node)
    if isinstance(node, list):
        for item in node:
            if isinstance(item, list):
                merged.update(collapse(item))
            elif isinstance(item, Mapping):
                merged.update(item)
    return merged


def _first_list(node: object) -> list:
    """Return the first ``list`` member of ``node`` (used to find a kv block).

    Yahoo entities frequently look like ``[<kv list>, {sub}, ...]``; the kv
    metadata block is the first list element. Returns ``[]`` if none.
    """
    if isinstance(node, list):
        for item in node:
            if isinstance(item, list):
                return item
    return []


# --------------------------------------------------------------------------- #
# Game / league discovery
# --------------------------------------------------------------------------- #


def parse_game(payload: Mapping) -> Game:
    """Parse ``/game/nfl`` → :class:`Game` (the current-season game key)."""
    content = unwrap(payload)
    game = collapse(content.get("game"))
    if not game.get("game_key"):
        raise YahooParseError("game payload missing 'game_key'")
    return _build_game(game)


def _build_game(game: RawDict) -> Game:
    # Yahoo marks the live in-season game as neither over nor in the offseason.
    is_current = not _bool(game.get("is_game_over")) and not _bool(
        game.get("is_offseason")
    )
    return Game(
        game_key=_str(game.get("game_key")),
        game_id=_str(game.get("game_id")),
        name=_str(game.get("name")),
        code=_str(game.get("code")),
        season=_str(game.get("season")),
        is_current=is_current,
        raw=game,
    )


def parse_user_leagues(payload: Mapping) -> list[League]:
    """Parse ``users;use_login=1/games/leagues`` → the user's leagues.

    Walks ``users -> user -> games -> game -> leagues -> league``, carrying each
    game's ``game_key``/``season`` down onto the leagues nested under it.
    """
    content = unwrap(payload)
    leagues: list[League] = []
    for user_node in iter_collection(content.get("users")):
        user = collapse(user_node.get("user")) if isinstance(user_node, Mapping) else {}
        for game_node in iter_collection(user.get("games")):
            game_entry = game_node.get("game") if isinstance(game_node, Mapping) else None
            game_meta = collapse(_first_list(game_entry) or game_entry)
            game_key = _str(game_meta.get("game_key"))
            season = _str(game_meta.get("season"))
            leagues_node = _find_subresource(game_entry, "leagues")
            for league_node in iter_collection(leagues_node):
                league_entry = (
                    league_node.get("league")
                    if isinstance(league_node, Mapping)
                    else None
                )
                meta = collapse(_first_list(league_entry) or league_entry)
                if meta.get("league_key"):
                    leagues.append(_build_league(meta, game_key, season))
    return leagues


def _find_subresource(entry: object, key: str) -> object:
    """Find a named sub-resource inside a ``[<kv list>, {sub}, ...]`` entry."""
    if isinstance(entry, Mapping):
        return entry.get(key)
    if isinstance(entry, list):
        for item in entry:
            if isinstance(item, Mapping) and key in item:
                return item[key]
    return None


def _dig_subresource(entry: object, key: str) -> object:
    """Like :func:`_find_subresource`, but also looks one collection level deep.

    Yahoo nests some sub-resources under a numeric ``"0"`` wrapper — e.g. a
    roster's ``players`` live at ``roster["0"]["players"]``, and a scoreboard's
    ``matchups`` at ``scoreboard["0"]["matchups"]``. This checks the direct
    location first, then each indexed child.
    """
    direct = _find_subresource(entry, key)
    if direct is not None:
        return direct
    for value in iter_collection(entry):
        found = _find_subresource(value, key)
        if found is not None:
            return found
    return None


def parse_leagues(payload: Mapping) -> list[League]:
    """Parse a ``.../leagues`` collection at the top level → leagues."""
    content = unwrap(payload)
    leagues: list[League] = []
    for league_node in iter_collection(content.get("leagues")):
        entry = league_node.get("league") if isinstance(league_node, Mapping) else None
        meta = collapse(_first_list(entry) or entry)
        if meta.get("league_key"):
            leagues.append(_build_league(meta))
    return leagues


def parse_league(payload: Mapping) -> League:
    """Parse a single ``/league/{key}`` (metadata only) → :class:`League`."""
    content = unwrap(payload)
    meta = collapse(_first_list(content.get("league")) or content.get("league"))
    if not meta.get("league_key"):
        raise YahooParseError("league payload missing 'league_key'")
    return _build_league(meta)


def _build_league(meta: RawDict, game_key: str = "", season: str = "") -> League:
    league_key = _str(meta.get("league_key"))
    return League(
        league_key=league_key,
        league_id=_str(meta.get("league_id")),
        name=_str(meta.get("name")),
        season=_str(meta.get("season")) or season,
        # The game key is always the league key's own prefix — correct even when
        # the league was fetched directly, with no parent game node to inherit.
        game_key=game_key or game_key_of(league_key),
        num_teams=_int(meta.get("num_teams")),
        scoring_type=_str(meta.get("scoring_type")),
        league_type=_str(meta.get("league_type")),
        draft_status=_str(meta.get("draft_status")),
        current_week=_int(meta.get("current_week")),
        start_week=_int(meta.get("start_week")),
        end_week=_int(meta.get("end_week")),
        url=_str(meta.get("url")),
        is_finished=_bool(meta.get("is_finished")),
        raw=meta,
    )


# --------------------------------------------------------------------------- #
# League settings + scoring
# --------------------------------------------------------------------------- #


def parse_league_settings(payload: Mapping) -> LeagueSettings:
    """Parse ``/league/{key}/settings`` → :class:`LeagueSettings`.

    The ``league`` node is ``[<league meta>, {"settings": [ {...} ]}]``. Scoring
    lives in two parallel lists that we join by ``stat_id``: ``stat_categories``
    (names/metadata) and ``stat_modifiers`` (the point values).
    """
    content = unwrap(payload)
    league_node = content.get("league")
    league_meta = collapse(_first_list(league_node) or league_node)
    settings_node = _find_subresource(league_node, "settings")
    settings = collapse(_first_list(settings_node) or settings_node)

    roster_positions = [
        _build_roster_position(collapse(item.get("roster_position")))
        for item in iter_collection(settings.get("roster_positions"))
        if isinstance(item, Mapping) and item.get("roster_position") is not None
    ]

    stat_categories = _build_stat_categories(
        settings.get("stat_categories"), settings.get("stat_modifiers")
    )

    return LeagueSettings(
        league_key=_str(league_meta.get("league_key")),
        scoring_type=_str(settings.get("scoring_type") or league_meta.get("scoring_type")),
        draft_type=_str(settings.get("draft_type")),
        is_auction_draft=_bool(settings.get("is_auction_draft")),
        uses_playoff=_bool(settings.get("uses_playoff")),
        playoff_start_week=_int(settings.get("playoff_start_week")),
        num_playoff_teams=_int(settings.get("num_playoff_teams")),
        num_playoff_consolation_teams=_int(
            settings.get("num_playoff_consolation_teams")
        ),
        uses_playoff_reseeding=_bool(settings.get("uses_playoff_reseeding")),
        uses_faab=_bool(settings.get("uses_faab")),
        waiver_type=_str(settings.get("waiver_type")),
        waiver_rule=_str(settings.get("waiver_rule")),
        waiver_time=_int(settings.get("waiver_time")),
        trade_end_date=_str(settings.get("trade_end_date")),
        trade_ratify_type=_str(settings.get("trade_ratify_type")),
        trade_reject_time=_int(settings.get("trade_reject_time")),
        uses_negative_points=_bool(settings.get("uses_negative_points")),
        max_teams=_int(settings.get("max_teams") or league_meta.get("num_teams")),
        roster_positions=roster_positions,
        stat_categories=stat_categories,
        raw=settings,
    )


def _build_roster_position(pos: RawDict) -> RosterPosition:
    position = _str(pos.get("position"))
    if "is_starting_position" in pos:
        starting = _bool(pos.get("is_starting_position"))
    else:
        # Older leagues omit the flag; bench/IR are the only non-starting slots.
        starting = position not in BENCH_POSITIONS
    return RosterPosition(
        position=position,
        count=_int(pos.get("count"), 0) or 0,
        position_type=_str(pos.get("position_type")),
        is_starting_position=starting,
    )


def _build_stat_categories(
    categories_node: object, modifiers_node: object
) -> list[StatCategory]:
    """Join stat metadata with point modifiers by ``stat_id``."""
    # modifiers: stat_id -> value
    modifiers: dict[int, float | None] = {}
    mod_stats = modifiers_node.get("stats") if isinstance(modifiers_node, Mapping) else None
    for item in iter_collection(mod_stats):
        stat = collapse(item.get("stat")) if isinstance(item, Mapping) else {}
        stat_id = _int(stat.get("stat_id"))
        if stat_id is not None:
            modifiers[stat_id] = _float(stat.get("value"))

    categories: list[StatCategory] = []
    cat_stats = categories_node.get("stats") if isinstance(categories_node, Mapping) else None
    for item in iter_collection(cat_stats):
        stat = collapse(item.get("stat")) if isinstance(item, Mapping) else {}
        stat_id = _int(stat.get("stat_id"))
        if stat_id is None:
            continue
        categories.append(
            StatCategory(
                stat_id=stat_id,
                name=_str(stat.get("name")),
                display_name=_str(stat.get("display_name")),
                position_type=_str(stat.get("position_type")),
                value=modifiers.get(stat_id),
                sort_order=_str(stat.get("sort_order")),
            )
        )

    # A modifier with no matching category still scores points (Yahoo does this
    # for a handful of defensive stats). Surface it rather than silently losing
    # points from the scoring profile M4 rescores against.
    known = {cat.stat_id for cat in categories}
    for stat_id, value in modifiers.items():
        if stat_id not in known:
            categories.append(StatCategory(stat_id=stat_id, value=value))

    return categories


# --------------------------------------------------------------------------- #
# Teams + managers + rosters
# --------------------------------------------------------------------------- #


def parse_league_teams(payload: Mapping) -> list[Team]:
    """Parse ``/league/{key}/teams`` → every :class:`Team` in the league."""
    content = unwrap(payload)
    teams_node = _find_subresource(content.get("league"), "teams")
    return [
        _build_team(collapse(node.get("team")))
        for node in iter_collection(teams_node)
        if isinstance(node, Mapping)
    ]


def _build_team(meta: RawDict) -> Team:
    managers = [
        _build_manager(collapse(m.get("manager")))
        for m in iter_collection(meta.get("managers"))
        if isinstance(m, Mapping) and m.get("manager") is not None
    ]
    return Team(
        team_key=_str(meta.get("team_key")),
        team_id=_str(meta.get("team_id")),
        name=_str(meta.get("name")),
        url=_str(meta.get("url")),
        is_owned_by_current_login=_bool(meta.get("is_owned_by_current_login")),
        waiver_priority=_int(meta.get("waiver_priority")),
        faab_balance=_int(meta.get("faab_balance")),
        number_of_moves=_int(meta.get("number_of_moves")),
        number_of_trades=_int(meta.get("number_of_trades")),
        managers=managers,
        raw=meta,
    )


def _build_manager(meta: RawDict) -> Manager:
    return Manager(
        manager_id=_str(meta.get("manager_id")),
        nickname=_str(meta.get("nickname")),
        guid=_str(meta.get("guid")),
        is_current_login=_bool(meta.get("is_current_login")),
    )


def parse_team_roster(payload: Mapping) -> TeamRoster:
    """Parse ``/team/{key}/roster`` → a :class:`TeamRoster`.

    The ``team`` node is ``[<team meta>, {"roster": {..., "0": {"players": ...}}}]``.
    Each player carries a ``selected_position`` sub-resource giving the slot they
    are started in for the week — that, plus the coverage week, is the start/sit
    state M6 optimizes over.
    """
    content = unwrap(payload)
    team_node = content.get("team")
    team_meta = collapse(_first_list(team_node) or team_node)
    roster = _find_subresource(team_node, "roster")
    roster_meta = collapse(roster) if roster is not None else {}
    # Yahoo nests the players collection under a numeric wrapper:
    # roster["0"]["players"]. Dig one level in.
    players_node = _dig_subresource(roster, "players")
    is_editable = _bool(roster_meta.get("is_editable"))

    slots: list[RosterSlot] = []
    for node in iter_collection(players_node):
        player_entry = node.get("player") if isinstance(node, Mapping) else None
        if player_entry is None:
            continue
        player = _build_player(player_entry)
        slots.append(
            RosterSlot(
                player=player,
                selected_position=player.selected_position,
                is_editable=is_editable,
            )
        )

    return TeamRoster(
        team_key=_str(team_meta.get("team_key")),
        week=_int(roster_meta.get("week")),
        coverage_type=_str(roster_meta.get("coverage_type")),
        is_editable=is_editable,
        slots=slots,
        raw=roster_meta,
    )


# --------------------------------------------------------------------------- #
# Players
# --------------------------------------------------------------------------- #


def parse_players(payload: Mapping) -> list[Player]:
    """Parse a ``.../players`` collection → :class:`Player` records.

    Works for both ``/league/{key}/players`` and the ``players`` sub-resource of
    a roster. Locates the ``players`` collection wherever it sits under
    ``fantasy_content`` (``league`` or ``team`` scoped).
    """
    content = unwrap(payload)
    players_node = _locate_players(content)
    return [
        _build_player(node.get("player"))
        for node in iter_collection(players_node)
        if isinstance(node, Mapping) and node.get("player") is not None
    ]


def _locate_players(content: Mapping) -> object:
    """Find the ``players`` collection under a league- or team-scoped payload."""
    if "players" in content:
        return content["players"]
    for scope in ("league", "team"):
        if scope in content:
            found = _find_subresource(content[scope], "players")
            if found is not None:
                return found
            # roster-nested: team -> roster -> "0" -> players
            roster = _find_subresource(content[scope], "roster")
            found = _dig_subresource(roster, "players")
            if found is not None:
                return found
    return None


def _build_player(player_entry: object) -> Player:
    """Build a :class:`Player` from Yahoo's ``[<kv list>, {sub}, ...]`` entry."""
    meta = collapse(player_entry)
    name = meta.get("name")
    name_map = name if isinstance(name, Mapping) else {}

    eligible = [
        _str(item.get("position"))
        for item in iter_collection(meta.get("eligible_positions"))
        if isinstance(item, Mapping) and item.get("position")
    ]

    owned = collapse(meta.get("percent_owned")) if meta.get("percent_owned") else {}
    selected = (
        collapse(meta.get("selected_position")) if meta.get("selected_position") else {}
    )
    status_full, injury_note = _parse_player_status(meta)
    headshot = collapse(meta.get("headshot")) if meta.get("headshot") else {}

    player = Player(
        player_key=_str(meta.get("player_key")),
        player_id=_str(meta.get("player_id")),
        full_name=_str(name_map.get("full")),
        first_name=_str(name_map.get("first")),
        last_name=_str(name_map.get("last")),
        ascii_full_name=_str(name_map.get("ascii_full")),
        editorial_team_abbr=_str(meta.get("editorial_team_abbr")),
        editorial_team_full_name=_str(meta.get("editorial_team_full_name")),
        display_position=_str(meta.get("display_position")),
        position_type=_str(meta.get("position_type")),
        primary_position=_str(meta.get("primary_position"))
        or (eligible[0] if eligible else ""),
        eligible_positions=eligible,
        uniform_number=_str(meta.get("uniform_number")),
        bye_week=_parse_bye_week(meta.get("bye_weeks")),
        status=_str(meta.get("status")),
        status_full=status_full,
        injury_note=injury_note,
        on_disabled_list=_bool(meta.get("on_disabled_list")),
        percent_owned=_float(owned.get("value")),
        percent_owned_delta=_float(owned.get("delta")),
        is_undroppable=_bool(meta.get("is_undroppable")),
        image_url=_str(meta.get("image_url")) or _str(headshot.get("url")),
        url=_str(meta.get("url")),
        selected_position=_str(selected.get("position")),
        selected_position_is_flex=_bool(selected.get("is_flex")),
        player_ranks=_build_player_ranks(meta.get("player_ranks")),
        draft_analysis=_build_draft_analysis(meta.get("draft_analysis")),
        points=_parse_player_points(meta.get("player_points")),
        raw=meta,
    )
    return player


def _parse_bye_week(bye_weeks: object) -> int | None:
    """Yahoo nests the bye week as ``{"bye_weeks": {"week": "10"}}``."""
    node = collapse(bye_weeks) if bye_weeks is not None else {}
    return _int(node.get("week"))


def _parse_player_points(player_points: object) -> float | None:
    """Extract the ``total`` from a ``player_points`` sub-resource."""
    node = collapse(player_points) if player_points is not None else {}
    return _float(node.get("total"))


def _build_player_ranks(player_ranks: object) -> list[PlayerRank]:
    """Parse the ``player_ranks`` sub-resource → :class:`PlayerRank` records.

    Yahoo's own ranks (``PR`` preseason, ``PS`` projected season, ``AR`` actual,
    ``OR`` overall) are a free market signal M4 blends alongside external ranks.
    """
    ranks: list[PlayerRank] = []
    for item in iter_collection(player_ranks):
        rank = collapse(item.get("player_rank")) if isinstance(item, Mapping) else {}
        if not rank:
            continue
        ranks.append(
            PlayerRank(
                rank_type=_str(rank.get("rank_type")),
                rank_value=_int(rank.get("rank_value")),
                rank_season=_str(rank.get("rank_season")),
            )
        )
    return ranks


def _build_draft_analysis(draft_analysis: object) -> DraftAnalysis | None:
    """Parse the ``draft_analysis`` sub-resource → :class:`DraftAnalysis`.

    ``average_pick`` is the μ of the ADP distribution M5's survival model needs
    (§4.3); ``None`` when the sub-resource was not requested.
    """
    if draft_analysis is None:
        return None
    node = collapse(draft_analysis)
    if not node:
        return None
    return DraftAnalysis(
        average_pick=_float(node.get("average_pick")),
        average_round=_float(node.get("average_round")),
        average_cost=_float(node.get("average_cost")),
        percent_drafted=_float(node.get("percent_drafted")),
    )


def _parse_player_status(meta: RawDict) -> tuple[str, str]:
    """Return ``(status_full, injury_note)`` from the various Yahoo fields."""
    status_full = _str(meta.get("status_full"))
    injury_note = _str(meta.get("injury_note"))
    return status_full, injury_note


# --------------------------------------------------------------------------- #
# Draft results
# --------------------------------------------------------------------------- #


def parse_draft_results(payload: Mapping) -> list[DraftPick]:
    """Parse ``/league/{key}/draftresults`` → ordered :class:`DraftPick` list.

    Returns picks sorted by pick number so M5's board is always in draft order.
    """
    content = unwrap(payload)
    node = _find_subresource(content.get("league"), "draft_results")
    if node is None:
        node = content.get("draft_results")

    picks: list[DraftPick] = []
    for item in iter_collection(node):
        result = collapse(item.get("draft_result")) if isinstance(item, Mapping) else {}
        if not result:
            continue
        picks.append(
            DraftPick(
                pick=_int(result.get("pick"), 0) or 0,
                round=_int(result.get("round"), 0) or 0,
                team_key=_str(result.get("team_key")),
                player_key=_str(result.get("player_key")),
                cost=_int(result.get("cost")),
                raw=result,
            )
        )
    picks.sort(key=lambda p: p.pick)
    return picks


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


def parse_transactions(payload: Mapping) -> list[Transaction]:
    """Parse ``/league/{key}/transactions`` → :class:`Transaction` records."""
    content = unwrap(payload)
    node = _find_subresource(content.get("league"), "transactions")
    if node is None:
        node = content.get("transactions")

    transactions: list[Transaction] = []
    for item in iter_collection(node):
        entry = item.get("transaction") if isinstance(item, Mapping) else None
        if entry is None:
            continue
        meta = collapse(_first_list(entry) or entry)
        players_node = _find_subresource(entry, "players")
        transactions.append(
            Transaction(
                transaction_key=_str(meta.get("transaction_key")),
                transaction_id=_str(meta.get("transaction_id")),
                type=_str(meta.get("type")),
                status=_str(meta.get("status")),
                timestamp=_int(meta.get("timestamp")),
                faab_bid=_int(meta.get("faab_bid")),
                trader_team_key=_str(meta.get("trader_team_key")),
                tradee_team_key=_str(meta.get("tradee_team_key")),
                players=_build_transaction_players(players_node),
                raw=meta,
            )
        )
    return transactions


def _build_transaction_players(players_node: object) -> list[TransactionPlayer]:
    """Build each moved player's leg of a transaction.

    ``transaction_data`` is the nastiest shape Yahoo emits: a bare object for
    some legs and a **one-element list wrapping that object** for others
    (typically the drop half of an add/drop). :func:`collapse` normalizes both,
    which is why there is no special-case here.
    """
    players: list[TransactionPlayer] = []
    for node in iter_collection(players_node):
        entry = node.get("player") if isinstance(node, Mapping) else None
        if entry is None:
            continue
        meta = collapse(entry)
        name = meta.get("name")
        name_map = name if isinstance(name, Mapping) else {}
        tx_data = collapse(meta.get("transaction_data"))
        players.append(
            TransactionPlayer(
                player_key=_str(meta.get("player_key")),
                player_id=_str(meta.get("player_id")),
                name=_str(name_map.get("full")),
                editorial_team_abbr=_str(meta.get("editorial_team_abbr")),
                display_position=_str(meta.get("display_position")),
                movement_type=_str(tx_data.get("type")),
                source_type=_str(tx_data.get("source_type")),
                source_team_key=_str(tx_data.get("source_team_key")),
                destination_type=_str(tx_data.get("destination_type")),
                destination_team_key=_str(tx_data.get("destination_team_key")),
            )
        )
    return players


# --------------------------------------------------------------------------- #
# Matchups / scoreboard
# --------------------------------------------------------------------------- #


def parse_scoreboard(payload: Mapping) -> list[Matchup]:
    """Parse ``/league/{key}/scoreboard`` → :class:`Matchup` records.

    Path: ``league -> scoreboard -> [<week>, {"0": {"matchups": ...}}] ->
    matchup -> [<meta>, {"0": {"teams": ...}}]``.
    """
    content = unwrap(payload)
    scoreboard = _find_subresource(content.get("league"), "scoreboard")
    matchups_node = _find_subresource(scoreboard, "matchups")
    if matchups_node is None and isinstance(scoreboard, Mapping):
        # scoreboard is sometimes {"0": {"matchups": ...}, "week": ...}
        for value in iter_collection(scoreboard):
            found = _find_subresource(value, "matchups")
            if found is not None:
                matchups_node = found
                break

    matchups: list[Matchup] = []
    for node in iter_collection(matchups_node):
        entry = node.get("matchup") if isinstance(node, Mapping) else None
        if entry is None:
            continue
        matchups.append(_build_matchup(entry))
    return matchups


def _build_matchup(entry: object) -> Matchup:
    meta = collapse(entry) if isinstance(entry, (list, Mapping)) else {}
    teams_node = _find_subresource(entry, "teams")
    if teams_node is None:
        # teams nested one level deeper under a numeric key
        for value in iter_collection(meta):
            found = _find_subresource(value, "teams")
            if found is not None:
                teams_node = found
                break

    teams = [
        _build_matchup_team(node.get("team"))
        for node in iter_collection(teams_node)
        if isinstance(node, Mapping) and node.get("team") is not None
    ]
    return Matchup(
        week=_int(meta.get("week")),
        week_start=_str(meta.get("week_start")),
        week_end=_str(meta.get("week_end")),
        status=_str(meta.get("status")),
        is_playoffs=_bool(meta.get("is_playoffs")),
        is_consolation=_bool(meta.get("is_consolation")),
        is_tied=_bool(meta.get("is_tied")),
        winner_team_key=_str(meta.get("winner_team_key")),
        teams=teams,
        raw=meta,
    )


def _build_matchup_team(team_entry: object) -> MatchupTeam:
    meta = collapse(team_entry)
    points = collapse(meta.get("team_points"))
    projected = collapse(meta.get("team_projected_points"))
    return MatchupTeam(
        team_key=_str(meta.get("team_key")),
        name=_str(meta.get("name")),
        points=_float(points.get("total")),
        projected_points=_float(projected.get("total")),
    )


__all__ = [
    "YahooParseError",
    "unwrap",
    "iter_collection",
    "collection_count",
    "collapse",
    "parse_game",
    "parse_user_leagues",
    "parse_leagues",
    "parse_league",
    "parse_league_settings",
    "parse_league_teams",
    "parse_team_roster",
    "parse_players",
    "parse_draft_results",
    "parse_transactions",
    "parse_scoreboard",
]
