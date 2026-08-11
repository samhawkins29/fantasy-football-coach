"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from fantasy_coach.config import Config, ConfigError, DEFAULT_SCOPE


def test_load_from_explicit_environ():
    env = {
        "YAHOO_CLIENT_ID": "cid",
        "YAHOO_CLIENT_SECRET": "secret",
        "YAHOO_REDIRECT_URI": "https://localhost:8000/callback",
        "YAHOO_SCOPE": "fspt-w",
        "YAHOO_LEAGUE_KEY": "449.l.123456",
        "ODDS_API_KEY": "odds",
    }
    cfg = Config.load(environ=env)
    assert cfg.yahoo_client_id == "cid"
    assert cfg.yahoo_client_secret == "secret"
    assert cfg.yahoo_scope == "fspt-w"
    assert cfg.yahoo_league_key == "449.l.123456"
    assert cfg.odds_api_key == "odds"
    assert cfg.has_oauth_credentials is True


def test_defaults_when_env_empty():
    cfg = Config.load(environ={})
    assert cfg.yahoo_client_id == ""
    assert cfg.yahoo_scope == DEFAULT_SCOPE
    assert cfg.yahoo_redirect_uri == "https://localhost:8000/callback"
    assert cfg.token_path == Path(".tokens.json")
    assert cfg.has_oauth_credentials is False


def test_values_are_stripped():
    cfg = Config.load(environ={"YAHOO_CLIENT_ID": "  cid  ", "YAHOO_SCOPE": "  "})
    assert cfg.yahoo_client_id == "cid"
    # blank scope stays blank (= omit the scope param; needed for Fantasy)
    assert cfg.yahoo_scope == ""


def test_custom_token_path():
    cfg = Config.load(environ={"FANTASY_COACH_TOKEN_PATH": "/tmp/custom.json"})
    assert cfg.token_path == Path("/tmp/custom.json")


def test_require_oauth_raises_and_names_missing():
    cfg = Config.load(environ={"YAHOO_CLIENT_ID": "cid"})
    with pytest.raises(ConfigError) as exc:
        cfg.require_oauth()
    msg = str(exc.value)
    assert "YAHOO_CLIENT_SECRET" in msg
    # redirect_uri has a default so it should NOT be reported missing
    assert "YAHOO_REDIRECT_URI" not in msg


def test_require_oauth_passes_when_complete():
    cfg = Config.load(
        environ={
            "YAHOO_CLIENT_ID": "cid",
            "YAHOO_CLIENT_SECRET": "secret",
        }
    )
    cfg.require_oauth()  # should not raise


def test_load_does_not_read_dotenv_when_environ_passed(tmp_path, monkeypatch):
    # Even if a .env exists, passing environ= bypasses it entirely.
    env_file = tmp_path / ".env"
    env_file.write_text("YAHOO_CLIENT_ID=from-dotenv\n", encoding="utf-8")
    cfg = Config.load(env_file=env_file, environ={"YAHOO_CLIENT_ID": "from-arg"})
    assert cfg.yahoo_client_id == "from-arg"
