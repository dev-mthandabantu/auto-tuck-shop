import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from utils.timing import track

logger = logging.getLogger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"

MAX_JSON_PARSE_RETRIES = 2


@dataclass
class CompletionResult:
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass
class ParseJsonResult:
    response: dict
    prompt_tokens: int | None
    completion_tokens: int | None


class OpenRouterError(Exception):
    """Exception raised for OpenRouter API errors."""

    pass


class TruncatedResponseError(OpenRouterError):
    """Raised when the LLM response was truncated (finish_reason=length)."""

    pass


def _is_retryable(exc: BaseException) -> bool:
    """Return True for transient HTTP errors worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 502, 503, 504)
    return isinstance(exc, httpx.RequestError)


_retry_policy = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)


class OpenRouterClient:
    """Client for interacting with the OpenRouter API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key or settings.OPENROUTER_API_KEY
        self.model = model or settings.OPENROUTER_MODEL

        if not self.api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not configured")

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://auto-tuck-shop.fly.dev",
            "X-Title": "Auto Tuckshop",
        }

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
    ) -> CompletionResult:
        """
        Send a chat completion request to OpenRouter.

        Args:
            messages: List of message dicts with 'role' and 'content' keys
            response_format: Optional response format specification for JSON mode

        Returns:
            CompletionResult with content and token counts
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }

        if response_format:
            payload["response_format"] = response_format

        @_retry_policy
        async def _do_request():
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    OPENROUTER_API_URL,
                    headers=self._get_headers(),
                    json=payload,
                )
                response.raise_for_status()
                return response

        try:
            response = await _do_request()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 402:
                gemini_key = getattr(settings, "GEMINI_API_KEY", "")
                if gemini_key:
                    logger.warning("OpenRouter 402 — falling back to Gemini direct API")
                    response = await self._gemini_fallback(payload, gemini_key)
                else:
                    logger.error("OpenRouter 402 and no GEMINI_API_KEY configured")
                    raise OpenRouterError("API request failed: 402") from e
            else:
                logger.error(f"OpenRouter HTTP error: {e.response.status_code} - {e.response.text}")
                raise OpenRouterError(f"API request failed: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"OpenRouter request error: {e}")
            raise OpenRouterError(f"Request failed: {e}") from e

        data = response.json()

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError) as e:
            logger.error(f"Unexpected OpenRouter response format: {data}")
            raise OpenRouterError("Unexpected response format") from e

        if finish_reason == "length":
            logger.warning(
                f"OpenRouter response truncated (finish_reason=length). "
                f"Content length: {len(content) if content else 0}"
            )
            raise TruncatedResponseError(
                f"Response truncated (finish_reason=length), content length: "
                f"{len(content) if content else 0}"
            )

        usage = data.get("usage", {})
        return CompletionResult(
            content=content,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    async def _gemini_fallback(self, payload: dict, gemini_key: str):
        """Call Gemini directly using its OpenAI-compatible endpoint."""
        fallback_payload = {**payload, "model": GEMINI_FALLBACK_MODEL}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                GEMINI_API_URL,
                headers={
                    "Authorization": f"Bearer {gemini_key}",
                    "Content-Type": "application/json",
                },
                json=fallback_payload,
            )
            response.raise_for_status()
            return response

    async def parse_json_response(
        self,
        messages: list[dict[str, str]],
    ) -> ParseJsonResult:
        """
        Send a chat completion request and parse the response as JSON.

        Retries up to MAX_JSON_PARSE_RETRIES times on truncated responses
        or invalid JSON, since these are transient LLM generation failures.

        Args:
            messages: List of message dicts with 'role' and 'content' keys

        Returns:
            ParseJsonResult with parsed response dict and token counts
        """
        last_error = None

        async with track("openrouter_llm"):
            for attempt in range(1 + MAX_JSON_PARSE_RETRIES):
                try:
                    completion = await self.chat_completion(
                        messages,
                        response_format={"type": "json_object"},
                    )
                except TruncatedResponseError as e:
                    last_error = e
                    logger.warning(
                        f"Truncated response on attempt {attempt + 1}/"
                        f"{1 + MAX_JSON_PARSE_RETRIES}, retrying"
                    )
                    continue

                try:
                    return ParseJsonResult(
                        response=json.loads(completion.content),
                        prompt_tokens=completion.prompt_tokens,
                        completion_tokens=completion.completion_tokens,
                    )
                except json.JSONDecodeError as e:
                    last_error = OpenRouterError(f"Invalid JSON response: {e}")
                    logger.warning(
                        f"Invalid JSON on attempt {attempt + 1}/"
                        f"{1 + MAX_JSON_PARSE_RETRIES}: {completion.content[:200]}"
                    )
                    continue

            # All retries exhausted
            logger.error(
                f"Failed to get valid JSON response after "
                f"{1 + MAX_JSON_PARSE_RETRIES} attempts"
            )
            raise last_error
