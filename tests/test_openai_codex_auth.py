from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from src.auth.openai_codex import (
    build_authorization_url,
    parse_authorization_input,
    save_credentials,
)


def test_build_authorization_url_uses_codex_oauth_parameters():
    url = build_authorization_url(
        challenge="challenge-value",
        state="state-value",
        originator="horizon",
    )

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/oauth/authorize"
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["app_EMoamEEZ73f0CkXaXp7hrann"]
    assert params["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert params["scope"] == ["openid profile email offline_access"]
    assert params["code_challenge"] == ["challenge-value"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == ["state-value"]
    assert params["codex_cli_simplified_flow"] == ["true"]
    assert params["originator"] == ["horizon"]


def test_parse_authorization_input_accepts_full_redirect_url():
    parsed = parse_authorization_input(
        "http://localhost:1455/auth/callback?code=abc123&state=state-value"
    )

    assert parsed == {"code": "abc123", "state": "state-value"}


def test_save_credentials_writes_expected_json(tmp_path):
    auth_file = tmp_path / "auth.json"

    save_credentials(
        auth_file,
        access="access-token",
        refresh="refresh-token",
        expires=123456,
        account_id="acct_123",
    )

    data = json.loads(auth_file.read_text(encoding="utf-8"))
    assert data == {
        "access": "access-token",
        "refresh": "refresh-token",
        "expires": 123456,
        "accountId": "acct_123",
    }
