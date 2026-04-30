"""AI client abstraction supporting multiple providers."""

import base64
import json
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from google import genai
from google.genai import types

from ..models import AIConfig, AIProvider
from .tokens import record_usage


CODEX_BACKEND_BASE_URL = "https://chatgpt.com/backend-api"
CODEX_AUTH_FILE_ENV = "HORIZON_CODEX_AUTH_FILE"
CODEX_ACCOUNT_CLAIM = "https://api.openai.com/auth"


class AIClient(ABC):
    """Abstract base class for AI clients."""

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion from AI model.

        Args:
            system: System prompt
            user: User prompt
            temperature: Optional sampling temperature override
            max_tokens: Optional maximum tokens override

        Returns:
            str: Generated completion text
        """
        pass


class AnthropicClient(AIClient):
    """Client for Anthropic Claude models."""

    def __init__(self, config: AIConfig):
        """Initialize Anthropic client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")

        kwargs = {"api_key": api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url

        self.client = AsyncAnthropic(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using Claude.

        Args:
            system: System prompt
            user: User prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            str: Generated text
        """
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        message = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}]
        )
        usage = getattr(message, "usage", None)
        if usage is not None:
            record_usage(
                "anthropic",
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
            )
        return message.content[0].text


class OpenAIClient(AIClient):
    """Client for OpenAI models."""

    def __init__(self, config: AIConfig):
        """Initialize OpenAI client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")

        kwargs = {"api_key": api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url

        self.client = AsyncOpenAI(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using OpenAI.

        Args:
            system: System prompt
            user: User prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            str: Generated text
        """
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            record_usage(
                "openai",
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            )
        return response.choices[0].message.content


class OpenAICodexClient(AIClient):
    """Client for ChatGPT Codex subscription OAuth responses."""

    def __init__(
        self,
        config: AIConfig,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.config = config
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens
        self.base_url = (config.base_url or CODEX_BACKEND_BASE_URL).rstrip("/")
        self.auth_file = self.resolve_auth_file()
        self._transport = transport

        if not self.auth_file.exists():
            raise ValueError(
                f"Missing OpenAI Codex OAuth credentials: {self.auth_file}. "
                "Run `uv run horizon-auth openai-codex` first."
            )

    @staticmethod
    def resolve_auth_file() -> Path:
        override = os.getenv(CODEX_AUTH_FILE_ENV)
        if override:
            return Path(override).expanduser()
        return Path.home() / ".horizon" / "openai-codex-auth.json"

    @staticmethod
    def extract_account_id(token: str) -> str:
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError("Invalid JWT")
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload.encode())
            claims = json.loads(decoded)
            account_id = claims.get(CODEX_ACCOUNT_CLAIM, {}).get("chatgpt_account_id")
        except Exception as exc:
            raise ValueError("Failed to extract ChatGPT account ID from token") from exc

        if not isinstance(account_id, str) or not account_id:
            raise ValueError("Failed to extract ChatGPT account ID from token")
        return account_id

    def _load_credentials(self) -> dict:
        with self.auth_file.open("r", encoding="utf-8") as fh:
            credentials = json.load(fh)
        if not isinstance(credentials, dict):
            raise ValueError(f"Invalid OpenAI Codex credentials: {self.auth_file}")
        if not credentials.get("access") or not credentials.get("refresh"):
            raise ValueError(f"Incomplete OpenAI Codex credentials: {self.auth_file}")
        return credentials

    def _save_credentials(self, credentials: dict) -> None:
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        self.auth_file.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
        try:
            os.chmod(self.auth_file, 0o600)
        except OSError:
            pass

    async def _refresh_credentials(self, credentials: dict) -> dict:
        refresh_token = credentials.get("refresh")
        if not refresh_token:
            raise ValueError(f"Missing refresh token in {self.auth_file}")

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://auth.openai.com/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                    "refresh_token": refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code >= 400:
            raise ValueError(
                f"OpenAI Codex token refresh failed: {response.status_code} {response.text}"
            )

        data = response.json()
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        expires_in = data.get("expires_in")
        if not access or not refresh or not isinstance(expires_in, (int, float)):
            raise ValueError("OpenAI Codex token refresh returned invalid data")

        next_credentials = {
            "access": access,
            "refresh": refresh,
            "expires": int(time.time() * 1000 + expires_in * 1000),
            "accountId": self.extract_account_id(access),
        }
        self._save_credentials(next_credentials)
        return next_credentials

    async def _get_credentials(self) -> dict:
        credentials = self._load_credentials()
        expires = credentials.get("expires")
        if isinstance(expires, (int, float)) and int(time.time() * 1000) < expires - 60_000:
            return credentials
        return await self._refresh_credentials(credentials)

    def _responses_url(self) -> str:
        if self.base_url.endswith("/codex/responses"):
            return self.base_url
        if self.base_url.endswith("/codex"):
            return f"{self.base_url}/responses"
        return f"{self.base_url}/codex/responses"

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        credentials = await self._get_credentials()
        access = credentials["access"]
        account_id = self.extract_account_id(access)

        payload = {
            "model": self.model,
            "store": False,
            "stream": True,
            "instructions": system,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user}],
                }
            ],
            "text": {"verbosity": "low"},
            "include": ["reasoning.encrypted_content"],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        headers = {
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": account_id,
            "originator": "horizon",
            "OpenAI-Beta": "responses=experimental",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "horizon",
        }

        async with httpx.AsyncClient(
            timeout=180,
            transport=self._transport,
        ) as client:
            response = await client.post(self._responses_url(), headers=headers, json=payload)
        if response.status_code >= 400:
            raise ValueError(
                f"OpenAI Codex response request failed: {response.status_code} {response.text}"
            )

        text, input_tokens, output_tokens = self._parse_sse_response(response.text)
        if input_tokens or output_tokens:
            record_usage("openai_codex", input_tokens=input_tokens, output_tokens=output_tokens)
        return text

    @staticmethod
    def _parse_sse_response(body: str) -> tuple[str, int, int]:
        chunks: list[str] = []
        input_tokens = 0
        output_tokens = 0
        for raw_event in body.split("\n\n"):
            data_lines = [
                line.removeprefix("data:").strip()
                for line in raw_event.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            raw_data = "\n".join(data_lines).strip()
            if not raw_data or raw_data == "[DONE]":
                continue
            try:
                event = json.loads(raw_data)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "response.output_text.delta":
                chunks.append(event.get("delta", ""))
            elif event_type in {"response.done", "response.completed"}:
                usage = event.get("response", {}).get("usage", {})
                input_tokens = int(usage.get("input_tokens", 0) or 0)
                output_tokens = int(usage.get("output_tokens", 0) or 0)
            elif event_type in {"error", "response.failed"}:
                message = (
                    event.get("message")
                    or event.get("response", {}).get("error", {}).get("message")
                    or json.dumps(event)
                )
                raise ValueError(f"OpenAI Codex response failed: {message}")
        return "".join(chunks), input_tokens, output_tokens


class MiniMaxClient(AIClient):
    """Client for MiniMax models via OpenAI-compatible API."""

    def __init__(self, config: AIConfig):
        """Initialize MiniMax client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")

        kwargs = {
            "api_key": api_key,
            "base_url": config.base_url or "https://api.minimax.io/v1",
        }

        self.client = AsyncOpenAI(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using MiniMax.

        MiniMax requires temperature in (0.0, 1.0] and does not support
        response_format, so we rely on prompt engineering for JSON output.

        Args:
            system: System prompt
            user: User prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            str: Generated text
        """
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        # MiniMax temperature must be in (0.0, 1.0]; clamp 0 to a small value
        if temperature <= 0:
            temperature = 0.01

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            record_usage(
                "minimax",
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            )
        return response.choices[0].message.content


class AliClient(AIClient):
    """Client for Alibaba DashScope (OpenAI-compatible API)."""

    def __init__(self, config: AIConfig):
        """Initialize DashScope client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")

        kwargs = {
            "api_key": api_key,
            "base_url": config.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }
        self.client = AsyncOpenAI(**kwargs)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using DashScope.

        Args:
            system: System prompt
            user: User prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            str: Generated text
        """
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        return response.choices[0].message.content


class GeminiClient(AIClient):
    """Client for Google Gemini models."""

    def __init__(self, config: AIConfig):
        """Initialize Gemini client.

        Args:
            config: AI configuration
        """
        self.config = config

        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key: {config.api_key_env}")

        self.client = genai.Client(api_key=api_key)
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate completion using Gemini.

        Args:
            system: System prompt
            user: User prompt
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            str: Generated text
        """
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
                max_output_tokens=max_tokens,
                response_mime_type="application/json"
            )
        )
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            total = getattr(usage, "total_token_count", 0) or 0
            prompt = getattr(usage, "prompt_token_count", 0) or 0
            completion = max(0, total - prompt)
            record_usage("gemini", input_tokens=prompt, output_tokens=completion)
        return response.text


def create_ai_client(config: AIConfig) -> AIClient:
    """Factory function to create appropriate AI client.

    Args:
        config: AI configuration

    Returns:
        AIClient: Initialized AI client

    Raises:
        ValueError: If provider is not supported
    """
    if config.provider == AIProvider.ANTHROPIC:
        return AnthropicClient(config)
    elif config.provider == AIProvider.OPENAI:
        return OpenAIClient(config)
    elif config.provider == AIProvider.OPENAI_CODEX:
        return OpenAICodexClient(config)
    elif config.provider == AIProvider.ALI:
        return AliClient(config)
    elif config.provider == AIProvider.GEMINI:
        return GeminiClient(config)
    elif config.provider == AIProvider.DOUBAO:
        return OpenAIClient(config)
    elif config.provider == AIProvider.MINIMAX:
        return MiniMaxClient(config)
    else:
        raise ValueError(f"Unsupported AI provider: {config.provider}")
