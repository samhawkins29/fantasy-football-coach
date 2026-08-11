"""CLI smoke tests — no network; token endpoint never called.

Uses Typer's CliRunner. We point the token store at a tmp path via the
FANTASY_COACH_TOKEN_PATH env var so nothing touches the real project files.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fantasy_coach.cli import _extract_code, _extract_state, app

runner = CliRunner()


# -- helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "pasted,expected",
    [
        ("ABC123", "ABC123"),
        ("  ABC123  ", "ABC123"),
        ("https://localhost:8000/callback?code=XYZ&state=s1", "XYZ"),
        ("?code=Q9&state=abc", "Q9"),
    ],
)
def test_extract_code(pasted, expected):
    assert _extract_code(pasted) == expected


def test_extract_state():
    url = "https://localhost:8000/callback?code=XYZ&state=s1"
    assert _extract_state(url) == "s1"
    assert _extract_state("just-a-code") is None


# -- commands ---------------------------------------------------------------


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("login", "status", "logout", "config"):
        assert cmd in result.stdout


def test_config_command_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("YAHOO_CLIENT_ID", "cid-123456")
    monkeypatch.setenv("YAHOO_CLIENT_SECRET", "secret-abcdef")
    monkeypatch.setenv("FANTASY_COACH_TOKEN_PATH", str(tmp_path / ".tokens.json"))
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "YAHOO_CLIENT_ID" in result.stdout
    # secret value itself must not be printed in full
    assert "secret-abcdef" not in result.stdout


def test_status_without_token_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("FANTASY_COACH_TOKEN_PATH", str(tmp_path / "none.json"))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "Not authenticated" in result.stdout


def test_login_without_creds_exits_nonzero(monkeypatch, tmp_path):
    # ensure no creds present
    for var in ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FANTASY_COACH_TOKEN_PATH", str(tmp_path / ".tokens.json"))
    # Config.load reads .env if present; force it to skip real env file effects
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 1
    assert "Configuration error" in result.stdout


def test_crosswalk_players_stamps_yahoo_ids(monkeypatch, id_map_rows):
    """_crosswalk_players resolves Yahoo identities and attaches ADP — the
    warm-path input that lets live picks (Yahoo ids) resolve to board rows."""
    from fantasy_coach import cli
    from fantasy_coach.ingest.crosswalk import load_id_crosswalk

    from .conftest import make_identity

    class StubYahooPlayer:
        def __init__(self, yahoo_id, name, team, position, adp):
            self.player_id = yahoo_id
            self.average_draft_pick = adp
            self._identity = make_identity(
                yahoo_player_id=yahoo_id, full_name=name,
                team_abbr=team, position=position,
            )

        def identity(self):
            return self._identity

    monkeypatch.setattr(
        "fantasy_coach.ingest.load_id_crosswalk",
        lambda: load_id_crosswalk(rows=id_map_rows),
    )
    players = cli._crosswalk_players(
        [
            StubYahooPlayer("30123", "Patrick Mahomes", "KC", "QB", 21.4),
            StubYahooPlayer("45001", "Freshman Rookie", "NYJ", "WR", None),
        ]
    )

    by_yahoo = {p.ids.yahoo_id: p for p in players}
    mahomes = by_yahoo["30123"]
    assert mahomes.canonical_id == "00-0033873"  # gsis hub id from the map
    assert mahomes.ids.sleeper_id  # spoke ids ride along for status merges
    assert mahomes.market.adp == 21.4
    # Unresolved players still carry their yahoo_id, so the pick resolves.
    assert by_yahoo["45001"].canonical_id.startswith("UNK_")
