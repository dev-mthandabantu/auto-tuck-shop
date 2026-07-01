import logging

import httpx
from django.conf import settings
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from utils.timing import track

logger = logging.getLogger(__name__)

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/speech-to-text"

# Biases transcription toward the currency/number vocabulary that shop owners
# actually use when stating sale prices — confirmed via spike testing (#230)
# to fix garbled decimal-price phrasing like "two dollars seventy" -> "Dola 70".
# Only supported on scribe_v2 (scribe_v1 rejects the keyterms parameter).
# Product names are appended by the caller (per-company, from the catalog) —
# spike testing showed biasing toward the actual product being sold (e.g.
# "Sasco") measurably improves accuracy beyond the currency vocabulary alone.
PRICE_VOCABULARY_KEYTERMS = [
    "dollars", "cents", "rand", "point", "each",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
    "hundred",
]

# ElevenLabs allows up to 1000 keyterms; leave headroom for the fixed
# vocabulary above plus per-company product names.
MAX_KEYTERMS = 1000


class ElevenLabsError(Exception):
    """Exception raised for Eleven Labs API errors."""

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


class ElevenLabsClient:
    """Client for interacting with the Eleven Labs API."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.ELEVENLABS_API_KEY

        if not self.api_key:
            raise ElevenLabsError("ELEVENLABS_API_KEY is not configured")

    def _get_headers(self) -> dict[str, str]:
        return {
            "xi-api-key": self.api_key,
        }

    async def transcribe_audio_from_url(self, audio_url: str, product_names: list[str] | None = None) -> str:
        """
        Transcribe audio from a URL using Eleven Labs Speech-to-Text API.

        Args:
            audio_url: HTTPS URL to the audio file
            product_names: Optional shop product names to bias transcription
                toward (e.g. so "Sasco" isn't misheard as "Sasuke")

        Returns:
            Transcribed text from the audio
        """
        async with track("elevenlabs_transcribe"):
            keyterms = PRICE_VOCABULARY_KEYTERMS + (product_names or [])
            data = {
                "model_id": "scribe_v2",
                "cloud_storage_url": audio_url,
                "keyterms": keyterms[:MAX_KEYTERMS],
            }

            @_retry_policy
            async def _do_request():
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        ELEVENLABS_API_URL,
                        headers=self._get_headers(),
                        data=data,  # Use form data, not JSON
                    )
                    response.raise_for_status()
                    return response

            try:
                response = await _do_request()
            except httpx.HTTPStatusError as e:
                logger.error(f"Eleven Labs HTTP error: {e.response.status_code} - {e.response.text}")
                raise ElevenLabsError(f"API request failed: {e.response.status_code}") from e
            except httpx.RequestError as e:
                logger.error(f"Eleven Labs request error: {e}")
                raise ElevenLabsError(f"Request failed: {e}") from e

            response_data = response.json()

            try:
                return response_data["text"]
            except KeyError as e:
                logger.error(f"Unexpected Eleven Labs response format: {response_data}")
                raise ElevenLabsError("Unexpected response format") from e

    async def transcribe_audio(
        self, audio_data: bytes, filename: str, product_names: list[str] | None = None
    ) -> str:
        """
        Transcribe audio using Eleven Labs Speech-to-Text API.

        Args:
            audio_data: Binary audio data
            filename: Filename with extension (e.g., "audio.ogg")
            product_names: Optional shop product names to bias transcription
                toward (e.g. so "Sasco" isn't misheard as "Sasuke")

        Returns:
            Transcribed text from the audio
        """
        async with track("elevenlabs_transcribe"):
            files = {
                "file": (filename, audio_data),
            }
            keyterms = PRICE_VOCABULARY_KEYTERMS + (product_names or [])
            data = {
                "model_id": "scribe_v2",
                "keyterms": keyterms[:MAX_KEYTERMS],
            }

            @_retry_policy
            async def _do_request():
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        ELEVENLABS_API_URL,
                        headers=self._get_headers(),
                        files=files,
                        data=data,
                    )
                    response.raise_for_status()
                    return response

            try:
                response = await _do_request()
            except httpx.HTTPStatusError as e:
                logger.error(f"Eleven Labs HTTP error: {e.response.status_code} - {e.response.text}")
                raise ElevenLabsError(f"API request failed: {e.response.status_code}") from e
            except httpx.RequestError as e:
                logger.error(f"Eleven Labs request error: {e}")
                raise ElevenLabsError(f"Request failed: {e}") from e

            response_data = response.json()

            try:
                return response_data["text"]
            except KeyError as e:
                logger.error(f"Unexpected Eleven Labs response format: {response_data}")
                raise ElevenLabsError("Unexpected response format") from e
