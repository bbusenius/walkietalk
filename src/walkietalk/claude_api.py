"""Explicitly billed Anthropic Messages API; no CLI or consumer credentials."""

import asyncio
import json
import os

import httpx

from .agent import SessionContext, validate_reply
from .config import Config, WalkietalkError

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
MAX_RESPONSE_BYTES = 65536
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 3,
}


class ClaudeApiAgent:
    def __init__(self, config: Config) -> None:
        self.config = config

    def label(self) -> str:
        return (
            f"claude_api ({self.config.claude_api_model}; "
            f"{self.config.claude_api_key_env}; billed API; "
            f"reasoning {self.config.claude_api_reasoning_effort})"
        )

    def reply(self, user_text: str, session_context: SessionContext) -> str:
        key = os.environ.get(self.config.claude_api_key_env, "")
        if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise WalkietalkError(
                f"claude_api requires a valid {self.config.claude_api_key_env} environment "
                "variable for the billed Anthropic API, not a Claude subscription or CLI login"
            )
        return asyncio.run(self._reply(user_text, session_context, key))

    async def _reply(self, user_text: str, context: SessionContext, key: str) -> str:
        messages = []
        for turn in context.history:
            messages.extend(
                [
                    {"role": "user", "content": turn.user_text},
                    {"role": "assistant", "content": turn.reply_text},
                ]
            )
        messages.append({"role": "user", "content": user_text})
        payload = {
            "model": self.config.claude_api_model,
            "system": context.instructions,
            "messages": messages,
            # Includes any reasoning tokens. The separate character cap still applies.
            "max_tokens": max(2048, self.config.agent_max_reply_chars * 2),
        }
        if self.config.agent_web_search:
            payload["tools"] = [WEB_SEARCH_TOOL]
        if self.config.claude_api_reasoning_effort != "default":
            payload["output_config"] = {"effort": self.config.claude_api_reasoning_effort}
        try:
            # One deadline covers connect, headers, and the entire body; no retries.
            async with asyncio.timeout(self.config.agent_timeout_seconds):
                async with httpx.AsyncClient(
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    trust_env=False,
                    follow_redirects=False,
                    timeout=None,
                ) as client:
                    async with client.stream("POST", MESSAGES_URL, json=payload) as response:
                        if response.status_code in {401, 403}:
                            raise WalkietalkError(
                                "Claude API key rejected or access denied "
                                f"(HTTP {response.status_code}); check billed Anthropic API access"
                            )
                        if response.status_code != 200:
                            raise WalkietalkError(
                                f"Claude API HTTP {response.status_code}; check API access, "
                                "model, effort, and service status. Server diagnostics withheld"
                            )
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise WalkietalkError(
                                    "Claude API response exceeded the transport limit; discarded"
                                )
                            body.extend(chunk)
        except TimeoutError:
            raise WalkietalkError(
                f"Claude API timed out after {self.config.agent_timeout_seconds:g}s; "
                "reply discarded"
            ) from None
        except httpx.HTTPError:
            raise WalkietalkError("Claude API connection failed; no reply retained") from None
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise WalkietalkError("Claude API returned malformed JSON; reply discarded") from None
        if (
            not isinstance(result, dict)
            or result.get("type") != "message"
            or result.get("role") != "assistant"
            or result.get("stop_reason") not in ("end_turn", "refusal")
            or not isinstance(result.get("content"), list)
        ):
            raise WalkietalkError("Claude API returned no complete final answer; reply discarded")
        parts = []
        for block in result["content"]:
            if not isinstance(block, dict):
                raise WalkietalkError("Claude API returned invalid content; reply discarded")
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif kind in ("thinking", "redacted_thinking"):
                continue
            elif self.config.agent_web_search and (
                (kind == "server_tool_use" and block.get("name") == "web_search")
                or kind == "web_search_tool_result"
            ):
                continue
            else:
                raise WalkietalkError("Claude API returned unexpected content; reply discarded")
        return validate_reply("\n".join(parts), self.config.agent_max_reply_chars)
