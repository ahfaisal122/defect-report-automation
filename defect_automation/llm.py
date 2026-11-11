"""One interface for every model call. The rest of the code never imports a provider SDK.

To add a provider: implement LLMClient and register it in get_llm_client (NFR-10).
"""

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from defect_automation.config import Settings

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cached: bool = False


class LLMClient(Protocol):
    model: str

    def generate_json(
        self, prompt: str, schema: type[BaseModel], temperature: float = 0.0
    ) -> LLMResponse:
        """Return the model's raw JSON text, constrained to the given schema."""
        ...


def _is_retryable(exc: BaseException) -> bool:
    return getattr(exc, "code", None) in RETRYABLE_STATUS


class GeminiClient:
    def __init__(self, api_key: str, model: str, timeout_seconds: float) -> None:
        from google import genai
        from google.genai import types

        # One attempt per SDK call: retries and backoff are handled here, in one place.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        self.model = model

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=4, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def generate_json(
        self, prompt: str, schema: type[BaseModel], temperature: float = 0.0
    ) -> LLMResponse:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=temperature,
            ),
        )
        usage = response.usage_metadata
        output_tokens = 0
        if usage:  # thinking tokens are billed as output
            output_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        return LLMResponse(
            text=response.text or "",
            model=self.model,
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=output_tokens,
        )


class CachingLLMClient:
    """Answers a repeated call from disk, so reruns and eval runs spend no quota.

    The key covers the model and the full prompt. The prompt already contains the prompt
    version's text and the report, so a change to any of them is a cache miss.
    """

    def __init__(self, inner: LLMClient, cache_dir: Path) -> None:
        self._inner = inner
        self._cache_dir = cache_dir
        self.model = inner.model

    def _path(self, prompt: str, schema: type[BaseModel], temperature: float) -> Path:
        key = "\x00".join([self.model, schema.__name__, str(temperature), prompt])
        return self._cache_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"

    def is_cached(self, prompt: str, schema: type[BaseModel], temperature: float = 0.0) -> bool:
        return self._path(prompt, schema, temperature).exists()

    def generate_json(
        self, prompt: str, schema: type[BaseModel], temperature: float = 0.0
    ) -> LLMResponse:
        path = self._path(prompt, schema, temperature)
        if path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
            return replace(LLMResponse(**stored), cached=True)
        response = self._inner.generate_json(prompt, schema, temperature)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(response)), encoding="utf-8")
        return response


def get_llm_client(settings: Settings, use_cache: bool = False) -> LLMClient:
    if settings.llm_provider != "gemini":
        raise ValueError(f"Unknown LLM provider: {settings.llm_provider!r}")
    if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
        raise RuntimeError("GEMINI_API_KEY is not set. Copy .env.example to .env and add it.")
    client: LLMClient = GeminiClient(
        settings.gemini_api_key.get_secret_value(),
        settings.gemini_model,
        settings.llm_timeout_seconds,
    )
    if use_cache and settings.llm_cache_enabled:
        client = CachingLLMClient(client, settings.llm_cache_dir)
    return client
