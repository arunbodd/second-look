"""Provider-agnostic LLM adapters.

One small interface, four implementations:
  * ``offline``   no model at all (the app uses templates and rule-based checks)
  * ``openai``    any OpenAI-compatible Chat Completions endpoint: OpenAI, Azure OpenAI,
                  OpenRouter, Groq, Together, vLLM, LM Studio, llama.cpp server, or Ollama
                  (``LLM_BASE_URL=http://localhost:11434/v1``)
  * ``anthropic`` the Anthropic Messages API
  * ``perplexity`` the Perplexity Agent API (``/v1/agent``), one key for OpenAI, Anthropic, Google,
                  and xAI models (model ids such as ``openai/gpt-6-luna``); web search stays off
  * ``litellm``   optional; routes through LiteLLM (100+ providers) if it is installed

Adapters use plain HTTP (httpx), so no vendor SDK is required.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from . import usage as usage_mod


class LLMError(RuntimeError):
    pass


@dataclass
class ProviderConfig:
    provider: str = "offline"
    model: str = ""
    base_url: str | None = None
    api_key: str | None = None
    timeout: float = 60.0
    temperature: float = 0.2
    max_tokens: int = 1400
    json_mode: str = "auto"  # openai only: auto | on | off
    reasoning_effort: str = "low"  # reasoning models: low keeps hidden (billed) reasoning tokens down; "" sends none

    @property
    def enabled(self) -> bool:
        return self.provider != "offline"

    def describe(self) -> str:
        if not self.enabled:
            return "offline"
        return f"{self.provider}:{self.model}"


@dataclass
class Completion:
    text: str
    model: str
    latency_ms: int
    usage: dict[str, Any]


class LLMProvider(Protocol):
    config: ProviderConfig

    def complete(self, system: str, messages: list[dict], json_output: bool = False,
                 temperature: float | None = None) -> Completion: ...


class OpenAICompatibleProvider:
    """Chat Completions adapter. Newer OpenAI models (gpt-5 and gpt-6 families, o-series) reject
    ``max_tokens`` and any non-default temperature; the adapter learns those quirks from the
    server's 400 responses once and keeps the working request shape for the rest of the session."""

    REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")

    def __init__(self, config: ProviderConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        base = (config.base_url or "https://api.openai.com/v1").rstrip("/")
        self._url = f"{base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = httpx.Client(timeout=config.timeout, headers=headers, transport=transport)
        self._json_supported = config.json_mode != "off"
        reasoning = config.model.lower().startswith(self.REASONING_PREFIXES) and not config.base_url
        self._token_param = "max_completion_tokens" if reasoning else "max_tokens"
        self._temperature_supported = not reasoning
        self._effort = config.reasoning_effort if reasoning else ""
        # Reasoning models spend part of the completion budget on hidden reasoning tokens.
        self._max_tokens = config.max_tokens * 3 if reasoning else config.max_tokens

    def _body(self, system, messages, json_output, temperature) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, *messages],
            self._token_param: self._max_tokens,
        }
        if self._temperature_supported:
            body["temperature"] = self.config.temperature if temperature is None else temperature
        if json_output and self._json_supported:
            body["response_format"] = {"type": "json_object"}
        if self._effort:
            body["reasoning_effort"] = self._effort
        return body

    def _adapt(self, error_text: str, body: dict) -> bool:
        """Adjust the request shape from a 400 response; True when a retry makes sense."""
        low = error_text.lower()
        if "max_tokens" in low and "max_completion_tokens" in low and self._token_param == "max_tokens":
            self._token_param = "max_completion_tokens"
            self._max_tokens = self.config.max_tokens * 3
            return True
        if "temperature" in low and "temperature" in body:
            self._temperature_supported = False
            return True
        if "reasoning_effort" in body and "reasoning" in low:
            self._effort = ""
            return True
        if "response_format" in body and ("response_format" in low or "json" in low) and self.config.json_mode == "auto":
            self._json_supported = False
            return True
        return False

    def complete(self, system, messages, json_output=False, temperature=None) -> Completion:
        t0 = time.perf_counter()
        for _ in range(4):
            body = self._body(system, messages, json_output, temperature)
            resp = post_with_retry(self._client, self._url, body)
            if resp.status_code == 400 and self._adapt(resp.text, body):
                continue
            break
        if resp.status_code >= 400:
            raise LLMError(f"{self.config.provider} returned HTTP {resp.status_code}: {_error_message(resp)}")
        data = resp.json()
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected response shape: {str(data)[:300]}") from exc
        if not text.strip() and choice.get("finish_reason") == "length":
            raise LLMError(f"{self.config.model} ran out of completion tokens before answering "
                           f"(budget {self._max_tokens}); raise LLM_MAX_TOKENS / JUDGE_MAX_TOKENS.")
        return Completion(text=text, model=data.get("model", self.config.model),
                          latency_ms=int((time.perf_counter() - t0) * 1000),
                          usage=usage_mod.normalize(data.get("usage"), self.config.model))


_sleep = time.sleep  # replaced in tests
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def post_with_retry(client: httpx.Client, url: str, body: dict, tries: int = 5) -> httpx.Response:
    """POST with backoff on rate limits (429), overload, and transient server or network errors.
    A rejected request (400, 401, 403, 404) is returned at once: retrying would not fix it."""
    delay = 1.0
    for attempt in range(tries):
        try:
            resp = client.post(url, json=body)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == tries - 1:
                raise
        else:
            if resp.status_code not in RETRY_STATUS or attempt == tries - 1:
                return resp
            try:
                delay = max(delay, min(30.0, float(resp.headers.get("retry-after", ""))))
            except ValueError:
                pass
        _sleep(delay + random.uniform(0, delay / 2))
        delay = min(delay * 2, 30.0)
    return resp


def _error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
    except ValueError:
        pass
    return resp.text[:300]


class AnthropicProvider:
    API_VERSION = "2023-06-01"

    def __init__(self, config: ProviderConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        base = (config.base_url or "https://api.anthropic.com").rstrip("/")
        self._url = f"{base}/v1/messages"
        headers = {"Content-Type": "application/json", "anthropic-version": self.API_VERSION}
        if config.api_key:
            headers["x-api-key"] = config.api_key
        self._client = httpx.Client(timeout=config.timeout, headers=headers, transport=transport)

    def complete(self, system, messages, json_output=False, temperature=None) -> Completion:
        if json_output:
            system = system + "\n\nRespond with a single JSON object and nothing else."
        # Prompt caching: the system prompt and the first user message (the evidence pack) are
        # marked cacheable, so a revision or a follow-up question on the same case reads them from
        # the cache at a tenth of the input price.
        msgs = [dict(m) for m in messages]
        if msgs and msgs[0]["role"] == "user" and isinstance(msgs[0]["content"], str):
            msgs[0]["content"] = [{"type": "text", "text": msgs[0]["content"], "cache_control": {"type": "ephemeral"}}]
        body = {
            "model": self.config.model,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": msgs,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature if temperature is None else temperature,
        }
        t0 = time.perf_counter()
        resp = post_with_retry(self._client, self._url, body)
        if resp.status_code >= 400:
            raise LLMError(f"anthropic returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return Completion(text=text, model=data.get("model", self.config.model),
                          latency_ms=int((time.perf_counter() - t0) * 1000),
                          usage=usage_mod.normalize(data.get("usage"), self.config.model))


class PerplexityAgentProvider:
    """Perplexity Agent API (OpenAI Responses shape). No tools are sent, so the model never searches
    the web and answers from the evidence pack alone. Request quirks (temperature, token limits) are
    learned from 400 responses the same way as the OpenAI adapter."""

    def __init__(self, config: ProviderConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        base = (config.base_url or "https://api.perplexity.ai/v1").rstrip("/")
        self._url = f"{base}/agent"
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = httpx.Client(timeout=config.timeout, headers=headers, transport=transport)
        # Perplexity rejects temperature for reasoning GPT models, and for Claude models together
        # with max_output_tokens (which Claude requires) with a bare "invalid request".
        self._temperature_supported = not config.model.lower().startswith(("openai/gpt-5", "openai/gpt-6", "anthropic/"))
        self._max_tokens = config.max_tokens * 3  # reasoning models spend part of the budget thinking
        self._token_param: str | None = "max_output_tokens"
        self._effort = config.reasoning_effort if config.model.lower().startswith(("openai/gpt-5", "openai/gpt-6")) else ""

    def _body(self, system, messages, json_output, temperature) -> dict[str, Any]:
        if json_output:
            system = system + "\n\nRespond with a single JSON object and nothing else."
        body: dict[str, Any] = {
            "model": self.config.model,
            "instructions": system,
            "input": [{"role": m["role"], "content": m["content"]} for m in messages],
        }
        if self._token_param:
            body[self._token_param] = self._max_tokens
        if self._temperature_supported:
            body["temperature"] = self.config.temperature if temperature is None else temperature
        if self._effort:
            body["reasoning"] = {"effort": self._effort}
        return body

    def _adapt(self, error_text: str, body: dict) -> bool:
        low = error_text.lower()
        if "reasoning" in body and "reasoning" in low:
            self._effort = ""
            return True
        if "temperature" in low and "temperature" in body:
            self._temperature_supported = False
            return True
        if self._token_param and self._token_param in low and "required" not in low:
            self._token_param = None
            return True
        if "required" in low and "max_output_tokens" in low and not self._token_param:
            self._token_param = "max_output_tokens"
            return True
        if "temperature" in body:  # a bare "invalid request": the optional parameter is the usual cause
            self._temperature_supported = False
            return True
        return False

    def complete(self, system, messages, json_output=False, temperature=None) -> Completion:
        t0 = time.perf_counter()
        for _ in range(4):
            body = self._body(system, messages, json_output, temperature)
            resp = post_with_retry(self._client, self._url, body)
            if resp.status_code == 400 and self._adapt(resp.text, body):
                continue
            break
        if resp.status_code >= 400:
            raise LLMError(f"perplexity returned HTTP {resp.status_code}: {_error_message(resp)}")
        data = resp.json()
        text = data.get("output_text") or ""
        if not text:
            parts = []
            for item in data.get("output") or []:
                if isinstance(item, dict) and item.get("type", "message") == "message":
                    for c in item.get("content") or []:
                        if isinstance(c, dict) and c.get("type") in ("output_text", "text") and c.get("text"):
                            parts.append(c["text"])
            text = "".join(parts)
        if not text.strip():
            raise LLMError(f"Perplexity returned no text for {self.config.model}: {str(data)[:300]}")
        return Completion(text=text, model=data.get("model", self.config.model),
                          latency_ms=int((time.perf_counter() - t0) * 1000),
                          usage=usage_mod.normalize(data.get("usage"), self.config.model))


class LiteLLMProvider:
    def __init__(self, config: ProviderConfig):
        try:
            import litellm  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMError("LLM_PROVIDER=litellm needs `pip install litellm`.") from exc
        self.config = config

    def complete(self, system, messages, json_output=False, temperature=None) -> Completion:  # pragma: no cover
        import litellm

        t0 = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens,
            "timeout": self.config.timeout,
        }
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        if self.config.base_url:
            kwargs["api_base"] = self.config.base_url
        resp = litellm.completion(**kwargs)
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        return Completion(text=text, model=getattr(resp, "model", self.config.model),
                          latency_ms=int((time.perf_counter() - t0) * 1000),
                          usage=usage_mod.normalize(dict(usage) if usage else {}, self.config.model))


def build_provider(config: ProviderConfig, transport: httpx.BaseTransport | None = None) -> LLMProvider | None:
    if not config.enabled:
        return None
    if config.provider == "openai":
        return OpenAICompatibleProvider(config, transport)
    if config.provider == "anthropic":
        return AnthropicProvider(config, transport)
    if config.provider == "perplexity":
        return PerplexityAgentProvider(config, transport)
    if config.provider == "litellm":
        return LiteLLMProvider(config)
    raise LLMError(f"Unknown LLM provider '{config.provider}'. Use offline, openai, anthropic, perplexity, or litellm.")


def extract_json(text: str) -> dict:
    """Parse the first JSON object in a model response (tolerates code fences and preambles)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise LLMError("The model response did not contain a valid JSON object.")
