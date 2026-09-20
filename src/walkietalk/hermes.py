"""Charlotte's Hermes Runs API: bounded requests, final text, explicit stop."""

import asyncio
import json
import os
import re
import sys

import httpx

from .agent import SessionContext
from .config import Config, WalkietalkError
from .term import emit

MAX_RESPONSE_BYTES = 65536
STOP_TIMEOUT_SECONDS = 2


class HermesAgent:
    def __init__(self, config: Config) -> None:
        self.config = config

    def label(self) -> str:
        return "hermes (Charlotte environment; text only)"

    def reply(self, user_text: str, session_context: SessionContext) -> str:
        token = os.environ.get(self.config.hermes_token_env, "")
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise WalkietalkError(
                f"Hermes requires a valid {self.config.hermes_token_env} environment variable; "
                "use the local Hermes API bearer token, not a provider key"
            )
        return asyncio.run(self._reply(user_text, session_context, token))

    async def _request(self, client, method, path, payload=None):
        # Never follow redirects or use ambient proxies with the local bearer token.
        async with client.stream(method, path, json=payload) as response:
            if response.status_code in {401, 403}:
                raise WalkietalkError(
                    "Hermes authentication denied; check the local API bearer token"
                )
            if response.status_code >= 300:
                raise WalkietalkError(
                    f"Hermes HTTP {response.status_code}; check the local service. "
                    "Server diagnostics were withheld"
                )
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise WalkietalkError("Hermes response exceeded the transport limit; discarded")
                data.extend(chunk)
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            raise WalkietalkError("Hermes returned malformed JSON; reply discarded") from None
        if not isinstance(result, dict):
            raise WalkietalkError("Hermes returned an invalid response; reply discarded")
        return result

    async def _reply(self, user_text, context, token):
        run_id = None
        terminal = False
        submitted = False
        async with httpx.AsyncClient(
            base_url=self.config.hermes_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            follow_redirects=False,
            timeout=None,  # asyncio.timeout bounds the entire operation, even a dripping body.
        ) as client:
            try:
                async with asyncio.timeout(self.config.agent_timeout_seconds):
                    capabilities = await self._request(client, "GET", "v1/capabilities")
                    features = capabilities.get("features")
                    if not isinstance(features, dict) or not all(
                        features.get(name) is True
                        for name in ("run_submission", "run_status", "run_stop")
                    ):
                        raise WalkietalkError(
                            "Hermes must support run submission, status, and stop"
                        )
                    history = []
                    for turn in context.history:
                        history.extend(
                            [
                                {"role": "user", "content": turn.user_text},
                                {"role": "assistant", "content": turn.reply_text},
                            ]
                        )
                    submitted = True
                    result = await self._request(
                        client,
                        "POST",
                        "v1/runs",
                        {
                            "input": user_text,
                            "instructions": context.instructions,
                            "session_id": "walkietalk-" + context.session_id,
                            "conversation_history": history,
                        },
                    )
                    candidate = result.get("run_id")
                    if not isinstance(candidate, str) or not re.fullmatch(
                        r"run_[A-Za-z0-9_-]+", candidate
                    ):
                        raise WalkietalkError("Hermes returned no valid run identifier")
                    run_id = candidate
                    while True:
                        result = await self._request(client, "GET", f"v1/runs/{run_id}")
                        if result.get("run_id") != run_id:
                            raise WalkietalkError("Hermes returned a mismatched run identifier")
                        status = result.get("status")
                        if not isinstance(status, str):
                            raise WalkietalkError("Hermes returned an invalid run status")
                        if status in {"completed", "failed", "cancelled", "interrupted"}:
                            terminal = True
                            if status != "completed":
                                raise WalkietalkError(
                                    f"Hermes run {status}; check Charlotte's local service and "
                                    "provider login. Server diagnostics were withheld"
                                )
                            answer = result.get("output")
                            if not isinstance(answer, str) or not answer.strip():
                                raise WalkietalkError("Hermes returned no final answer text")
                            return answer
                        if status == "waiting_for_approval":
                            raise WalkietalkError("Hermes needs permission; no approval granted")
                        if status not in {"queued", "running", "stopping"}:
                            raise WalkietalkError("Hermes returned an unknown run status")
                        await asyncio.sleep(0.2)
            except TimeoutError:
                raise WalkietalkError(
                    f"Hermes timed out after {self.config.agent_timeout_seconds:g}s; "
                    "reply discarded"
                ) from None
            except httpx.HTTPError:
                raise WalkietalkError(
                    "Hermes connection failed; check the local API service and URL"
                ) from None
            finally:
                if run_id and not terminal:
                    try:
                        async with asyncio.timeout(STOP_TIMEOUT_SECONDS):
                            await self._request(client, "POST", f"v1/runs/{run_id}/stop", {})
                    except (WalkietalkError, httpx.HTTPError, TimeoutError):
                        emit(
                            "warn",
                            "Hermes stop could not be confirmed; check the local service.",
                            file=sys.stderr,
                        )
                elif submitted and not run_id:
                    emit(
                        "warn",
                        "Hermes submission was not acknowledged; server work may still be running. "
                        "Check the local service before retrying.",
                        file=sys.stderr,
                    )
