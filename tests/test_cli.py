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
