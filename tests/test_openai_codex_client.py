from __future__ import annotations

import base64
import asyncio
import json
import time
from pathlib import Path

import httpx

from src.ai.client import OpenAICodexClient, create_ai_client
from src.models import AIConfig, AIProvider


def _jwt_with_account(account_id: str) -> str:
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _write_auth(path: Path, *, access: str | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "access": access or _jwt_with_account("acct_test"),
                "refresh": "refresh-token",
                "expires": int((time.time() + 3600) * 1000),
                "accountId": "acct_test",
            }
        ),
        encoding="utf-8",
    )


def _make_config() -> AIConfig:
    return AIConfig(
        provider=AIProvider.OPENAI_CODEX,
        model="gpt-5.2",
        api_key_env="OPENAI_API_KEY",
        temperature=0.3,
        max_tokens=4096,
    )


def test_openai_codex_provider_factory(monkeypatch, tmp_path):
    auth_file = tmp_path / "codex-auth.json"
    _write_auth(auth_file)
    monkeypatch.setenv("HORIZON_CODEX_AUTH_FILE", str(auth_file))

    client = create_ai_client(_make_config())

    assert isinstance(client, OpenAICodexClient)
    assert AIProvider.OPENAI_CODEX.value == "openai_codex"


def test_extract_account_id_from_access_token():
    token = _jwt_with_account("acct_123")

    assert OpenAICodexClient.extract_account_id(token) == "acct_123"


def test_codex_complete_posts_to_codex_responses_and_parses_sse(monkeypatch, tmp_path):
    auth_file = tmp_path / "codex-auth.json"
    access = _jwt_with_account("acct_from_token")
    _write_auth(auth_file, access=access)
    monkeypatch.setenv("HORIZON_CODEX_AUTH_FILE", str(auth_file))

    seen_request = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_request["url"] = str(request.url)
        seen_request["headers"] = dict(request.headers)
        seen_request["body"] = json.loads(request.content.decode())
        payload = "\n\n".join(
            [
                'data: {"type":"response.output_item.added","item":{"type":"message"}}',
                'data: {"type":"response.content_part.added","part":{"type":"output_text","text":""}}',
                'data: {"type":"response.output_text.delta","delta":"{\\"ok\\":"}',
                'data: {"type":"response.output_text.delta","delta":" true}"}',
                'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":4,"output_tokens":3,"total_tokens":7}}}',
            ]
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"{payload}\n\n".encode(),
        )

    client = OpenAICodexClient(_make_config(), transport=httpx.MockTransport(handler))

    result = asyncio.run(client.complete(system="Return JSON only.", user="Say ok"))

    assert result == '{"ok": true}'
    assert seen_request["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert seen_request["headers"]["authorization"] == f"Bearer {access}"
    assert seen_request["headers"]["chatgpt-account-id"] == "acct_from_token"
    assert seen_request["headers"]["openai-beta"] == "responses=experimental"
    assert seen_request["body"]["model"] == "gpt-5.2"
    assert seen_request["body"]["instructions"] == "Return JSON only."
    assert "temperature" not in seen_request["body"]
    assert seen_request["body"]["input"][0]["role"] == "user"
    assert seen_request["body"]["input"][0]["content"][0]["text"] == "Say ok"
