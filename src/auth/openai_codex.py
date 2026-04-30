"""OpenAI Codex OAuth login for Horizon."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv
from rich.console import Console

from ..ai.client import CODEX_AUTH_FILE_ENV, OpenAICodexClient


CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"

console = Console()


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = _base64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def build_authorization_url(
    *,
    challenge: str,
    state: str,
    originator: str = "horizon",
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": originator,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def parse_authorization_input(value: str) -> dict[str, Optional[str]]:
    value = value.strip()
    if not value:
        return {"code": None, "state": None}

    try:
        parsed = urlparse(value)
        if parsed.query:
            params = parse_qs(parsed.query)
            return {
                "code": params.get("code", [None])[0],
                "state": params.get("state", [None])[0],
            }
    except ValueError:
        pass

    if "#" in value:
        code, state = value.split("#", 1)
        return {"code": code, "state": state}

    if "code=" in value:
        params = parse_qs(value)
        return {
            "code": params.get("code", [None])[0],
            "state": params.get("state", [None])[0],
        }

    return {"code": value, "state": None}


def save_credentials(
    auth_file: Path,
    *,
    access: str,
    refresh: str,
    expires: int,
    account_id: str,
) -> None:
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(
        json.dumps(
            {
                "access": access,
                "refresh": refresh,
                "expires": expires,
                "accountId": account_id,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(auth_file, 0o600)
    except OSError:
        pass


class _OAuthCallbackServer:
    def __init__(self, state: str):
        self.state = state
        self.code: Optional[str] = None
        self.error: Optional[str] = None
        self.event = threading.Event()
        self.httpd: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # noqa: N802
                return

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != "/auth/callback":
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Callback route not found.")
                    return

                params = parse_qs(parsed.query)
                callback_state = params.get("state", [None])[0]
                if callback_state != outer.state:
                    outer.error = "State mismatch."
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"State mismatch.")
                    outer.event.set()
                    return

                code = params.get("code", [None])[0]
                if not code:
                    outer.error = "Missing authorization code."
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Missing authorization code.")
                    outer.event.set()
                    return

                outer.code = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"OpenAI authentication completed. You can close this window.")
                outer.event.set()

        try:
            self.httpd = HTTPServer(("127.0.0.1", 1455), Handler)
        except OSError:
            return False

        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return True

    def close(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


async def exchange_authorization_code(code: str, verifier: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Token exchange failed: {response.status_code} {response.text}")
    data = response.json()
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    expires_in = data.get("expires_in")
    if not access or not refresh or not isinstance(expires_in, (int, float)):
        raise RuntimeError("Token exchange returned invalid data.")

    return {
        "access": access,
        "refresh": refresh,
        "expires": int(time.time() * 1000 + expires_in * 1000),
        "accountId": OpenAICodexClient.extract_account_id(access),
    }


async def login_openai_codex(auth_file: Optional[Path] = None) -> Path:
    verifier, challenge = generate_pkce()
    state = secrets.token_hex(16)
    url = build_authorization_url(challenge=challenge, state=state)
    callback_server = _OAuthCallbackServer(state)
    callback_enabled = callback_server.start()

    console.print("\n[bold]OpenAI Codex OAuth login[/bold]\n")
    console.print("Open this URL in your browser:\n")
    console.print(url)
    console.print()
    webbrowser.open(url)

    code: Optional[str] = None
    try:
        if callback_enabled:
            console.print("Waiting for browser callback on http://127.0.0.1:1455/auth/callback ...")
            callback_server.event.wait(timeout=180)
            if callback_server.error:
                raise RuntimeError(callback_server.error)
            code = callback_server.code

        if not code:
            console.print("\nPaste the authorization code or full redirect URL:")
            parsed = parse_authorization_input(input("> "))
            if parsed.get("state") and parsed["state"] != state:
                raise RuntimeError("State mismatch.")
            code = parsed.get("code")

        if not code:
            raise RuntimeError("Missing authorization code.")

        credentials = await exchange_authorization_code(code, verifier)
        target = auth_file or OpenAICodexClient.resolve_auth_file()
        save_credentials(
            target,
            access=credentials["access"],
            refresh=credentials["refresh"],
            expires=credentials["expires"],
            account_id=credentials["accountId"],
        )
        console.print(f"\n[green]Saved OpenAI Codex credentials to {target}[/green]")
        return target
    finally:
        callback_server.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Horizon OAuth authentication")
    parser.add_argument(
        "provider",
        choices=["openai-codex"],
        help="OAuth provider to authenticate",
    )
    parser.add_argument(
        "--auth-file",
        type=Path,
        default=None,
        help=f"Override credential file path. Defaults to ${CODEX_AUTH_FILE_ENV} or ~/.horizon/openai-codex-auth.json.",
    )
    args = parser.parse_args()

    import asyncio

    asyncio.run(login_openai_codex(args.auth_file))


if __name__ == "__main__":
    main()
