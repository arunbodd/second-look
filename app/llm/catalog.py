"""Catalogue of current models the UI can switch between at runtime.

Only current top-tier models from the three families are listed (verified September 2026).
Prices are USD per million tokens (input / output) and are only used for the estimate shown
before a run. Anything configured in .env that is not listed here is added as a custom entry
so that an existing setup keeps working.
"""

from __future__ import annotations

import os

from .providers import ProviderConfig

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

FAMILIES = {
    "openai": {"label": "OpenAI", "key_env": "OPENAI_API_KEY", "provider": "openai", "base_url": None},
    "anthropic": {"label": "Anthropic", "key_env": "ANTHROPIC_API_KEY", "provider": "anthropic", "base_url": None},
    "gemini": {"label": "Google", "key_env": "GEMINI_API_KEY", "provider": "openai", "base_url": GEMINI_BASE_URL},
    # One Perplexity key reaches models from several vendors through the Agent API.
    "perplexity": {"label": "Perplexity", "key_env": "PERPLEXITY_API_KEY", "provider": "perplexity", "base_url": None},
}

# id, label, family, model string, price in, price out, roles it is recommended for
CATALOG: list[dict] = [
    {"id": "gpt-6-luna", "label": "GPT-6 Luna", "family": "openai", "model": "gpt-6-luna", "in": 0.10, "out": 0.50, "roles": ["writer"]},
    {"id": "gpt-6-sol", "label": "GPT-6 Sol", "family": "openai", "model": "gpt-6-sol", "in": 2.0, "out": 10.0, "roles": ["writer", "judge"]},
    {"id": "gpt-6-astra", "label": "GPT-6 Astra", "family": "openai", "model": "gpt-6-astra", "in": 10.0, "out": 50.0, "roles": ["writer", "judge"]},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "family": "anthropic", "model": "claude-haiku-4-5", "in": 1.0, "out": 5.0, "roles": ["writer"]},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "family": "anthropic", "model": "claude-sonnet-5", "in": 2.0, "out": 10.0, "roles": ["writer", "judge"]},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "family": "anthropic", "model": "claude-opus-5-5", "in": 4.0, "out": 20.0, "roles": ["writer", "judge"]},
    {"id": "claude-fable-5-1", "label": "Claude Fable 5.1", "family": "anthropic", "model": "claude-fable-5-1", "in": 10.0, "out": 50.0, "roles": ["writer", "judge"]},
    {"id": "gemini-3-8-flash", "label": "Gemini 3.8 Flash", "family": "gemini", "model": "gemini-3.8-flash", "in": 0.75, "out": 3.75, "roles": ["writer"]},
    {"id": "gemini-3-1-pro", "label": "Gemini 3.1 Pro", "family": "gemini", "model": "gemini-3.1-pro-preview", "in": 2.0, "out": 12.0, "roles": ["writer", "judge"]},
    # Through Perplexity's Agent API (prices from its model list; the lower tier is shown).
    {"id": "pplx-gpt-6-luna", "label": "GPT-6 Luna via Perplexity", "family": "perplexity", "vendor": "openai", "model": "openai/gpt-6-luna", "in": 0.10, "out": 0.50, "roles": ["writer"]},
    {"id": "pplx-gpt-6-sol", "label": "GPT-6 Sol via Perplexity", "family": "perplexity", "vendor": "openai", "model": "openai/gpt-6-sol", "in": 2.0, "out": 10.0, "roles": ["writer", "judge"]},
    {"id": "pplx-claude-haiku-4-5", "label": "Claude Haiku 4.5 via Perplexity", "family": "perplexity", "vendor": "anthropic", "model": "anthropic/claude-haiku-4-5", "in": 1.0, "out": 5.0, "roles": ["writer"]},
    {"id": "pplx-claude-sonnet-5", "label": "Claude Sonnet 5 via Perplexity", "family": "perplexity", "vendor": "anthropic", "model": "anthropic/claude-sonnet-5", "in": 2.0, "out": 10.0, "roles": ["writer", "judge"]},
    {"id": "pplx-claude-opus-5-5", "label": "Claude Opus 5.5 via Perplexity", "family": "perplexity", "vendor": "anthropic", "model": "anthropic/claude-opus-5-5", "in": 4.0, "out": 20.0, "roles": ["writer", "judge"]},
    {"id": "pplx-gemini-3-1-flash-lite", "label": "Gemini 3.1 Flash-Lite via Perplexity", "family": "perplexity", "vendor": "gemini", "model": "google/gemini-3.1-flash-lite", "in": 0.25, "out": 1.50, "roles": ["writer", "judge"]},
    {"id": "pplx-gemini-3-8-flash", "label": "Gemini 3.8 Flash via Perplexity", "family": "perplexity", "vendor": "gemini", "model": "google/gemini-3.8-flash", "in": 0.75, "out": 3.75, "roles": ["writer"]},
    {"id": "pplx-gemini-3-1-pro", "label": "Gemini 3.1 Pro via Perplexity", "family": "perplexity", "vendor": "gemini", "model": "google/gemini-3.1-pro-preview", "in": 2.0, "out": 12.0, "roles": ["writer", "judge"]},
]
for _e in CATALOG:  # the model maker, for the same-family warning (Perplexity only routes)
    _e.setdefault("vendor", _e["family"])


def vendor_of(provider: str, model: str, base_url: str | None = None) -> str:
    """Who made the model, whatever route it takes: a judge should come from another maker."""
    if provider == "perplexity" and "/" in model:
        prefix = model.split("/", 1)[0]
        return {"google": "gemini"}.get(prefix, prefix)
    if base_url and "generativelanguage" in base_url:
        return "gemini"
    return provider

# Per-case token budget for the pre-run estimate, before any case has been reviewed: the compact
# evidence pack averages about 4,600 tokens, the system prompt about 550, the judge also reads the
# draft, and one case in three needs a revision. Once reviews exist the estimate uses their
# measured averages instead.
CALLS_PER_CASE = 2.3
TOKENS_PER_CASE = {"writer": {"in": 6800, "out": 1300}, "judge": {"in": 7500, "out": 700}}


def key_status() -> dict[str, bool]:
    return {fam: bool(os.environ.get(spec["key_env"], "").strip()) for fam, spec in FAMILIES.items()}


def custom_entry(config: ProviderConfig) -> dict | None:
    """Represent a model configured in .env that is not in the catalogue."""
    if not config.enabled or not config.model:
        return None
    family = "gemini" if (config.base_url and "generativelanguage" in config.base_url) else config.provider
    if family not in FAMILIES:
        family = "openai"
    return {"id": f"custom:{config.provider}:{config.model}", "label": f"{config.model} (from .env)", "family": family,
            "vendor": vendor_of(config.provider, config.model, config.base_url),
            "model": config.model, "in": None, "out": None, "roles": ["writer", "judge"], "custom": True,
            "provider": config.provider, "base_url": config.base_url, "api_key": config.api_key}


def find(model_id: str, extras: list[dict] | None = None) -> dict | None:
    for entry in [*CATALOG, *(extras or [])]:
        if entry["id"] == model_id:
            return entry
    return None


def config_for(entry: dict, base: ProviderConfig) -> ProviderConfig:
    """Build a ProviderConfig for a catalogue entry, keeping the role's timeout/temperature."""
    if entry.get("custom"):
        return ProviderConfig(provider=entry["provider"], model=entry["model"], base_url=entry.get("base_url"),
                              api_key=entry.get("api_key"), timeout=base.timeout, temperature=base.temperature,
                              max_tokens=base.max_tokens, json_mode=base.json_mode, reasoning_effort=base.reasoning_effort)
    fam = FAMILIES[entry["family"]]
    return ProviderConfig(provider=fam["provider"], model=entry["model"], base_url=fam["base_url"],
                          api_key=os.environ.get(fam["key_env"], "").strip() or None, timeout=base.timeout,
                          temperature=base.temperature, max_tokens=base.max_tokens, json_mode=base.json_mode, reasoning_effort=base.reasoning_effort)


def entry_id_for(config: ProviderConfig) -> str | None:
    """The catalogue id matching a configured provider, if any."""
    if not config.enabled:
        return None
    for entry in CATALOG:
        fam = FAMILIES[entry["family"]]
        if entry["model"] == config.model and fam["provider"] == config.provider and (fam["base_url"] or None) == (config.base_url or None):
            return entry["id"]
    custom = custom_entry(config)
    return custom["id"] if custom else None
