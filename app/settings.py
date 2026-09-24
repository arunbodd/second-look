"""Configuration from environment variables (a local .env file is read if present).

Everything has a working default: with no configuration at all the app runs fully offline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .llm.providers import ProviderConfig

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5", "openai": "", "perplexity": "openai/gpt-6-luna", "litellm": ""}
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "perplexity": "PERPLEXITY_API_KEY", "litellm": ""}


API_KEY_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY")
KEY_SOURCE: dict[str, str] = {}  # where each API key came from: ".env" or "shell"


def _read_dotenv(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _apply_keys(values: dict[str, str]) -> None:
    """API keys in the project's .env win over keys exported in the shell. A key exported for
    another tool (in ~/.zshrc, say) would otherwise shadow the one written for this app."""
    for key in API_KEY_NAMES:
        if values.get(key):
            os.environ[key] = values[key]
            KEY_SOURCE[key] = ".env"
        elif os.environ.get(key):
            KEY_SOURCE[key] = "shell"
        else:
            KEY_SOURCE.pop(key, None)


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if os.environ.get("JAI_NO_DOTENV") or not path.exists():  # tests set JAI_NO_DOTENV so a local .env cannot leak in
        return
    values = _read_dotenv(path)
    for key, value in values.items():
        if key not in API_KEY_NAMES:
            os.environ.setdefault(key, value)
    _apply_keys(values)


def reload_api_keys(path: Path = ROOT / ".env") -> None:
    """Re-read the API keys from .env, so a key added or replaced while the app runs is used
    the next time models are picked, without a restart."""
    if os.environ.get("JAI_NO_DOTENV") or not path.exists():
        return
    _apply_keys(_read_dotenv(path))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _provider(prefix: str, fallback: ProviderConfig | None = None) -> ProviderConfig:
    provider = _env(f"{prefix}_PROVIDER", fallback.provider if fallback else "offline").lower() or "offline"
    same_as_fallback = fallback is not None and provider == fallback.provider
    model = _env(f"{prefix}_MODEL") or (fallback.model if same_as_fallback else DEFAULT_MODELS.get(provider, ""))
    base_url = _env(f"{prefix}_BASE_URL") or (fallback.base_url if same_as_fallback else None) or None
    api_key = (
        _env(f"{prefix}_API_KEY")
        or (fallback.api_key if same_as_fallback else None)
        or (_env(KEY_ENV.get(provider, "")) if KEY_ENV.get(provider) else "")
        or None
    )
    return ProviderConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout=float(_env(f"{prefix}_TIMEOUT") or (fallback.timeout if fallback else 90)),
        temperature=float(_env(f"{prefix}_TEMPERATURE") or (0.0 if prefix == "JUDGE" else 0.2)),
        max_tokens=int(_env(f"{prefix}_MAX_TOKENS") or 1400),
        json_mode=_env(f"{prefix}_JSON_MODE") or "auto",
        reasoning_effort=(_env(f"{prefix}_REASONING_EFFORT") or "low").lower().replace("none", ""),
    )


@dataclass
class Settings:
    generator: ProviderConfig
    judge: ProviderConfig
    data_path: Path
    db_path: Path
    precompute: bool
    max_concurrency: int
    max_revisions: int
    judge_chat: bool
    recorded_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return "offline" if not self.generator.enabled else "llm"


def load_settings() -> Settings:
    load_dotenv()
    warnings: list[str] = []
    generator = _provider("LLM")
    judge = _provider("JUDGE", fallback=generator)
    judge_enabled = _env("JUDGE_ENABLED", "true").lower() not in ("0", "false", "no", "off")
    if not judge_enabled:
        judge = ProviderConfig(provider="offline")

    for name, cfg in (("LLM", generator), ("JUDGE", judge)):
        if cfg.enabled and not cfg.model:
            warnings.append(f"{name}_PROVIDER={cfg.provider} needs {name}_MODEL. Running that role offline instead.")
            cfg.provider = "offline"
        if cfg.enabled and cfg.provider in ("openai", "anthropic", "perplexity") and not cfg.api_key and not (cfg.base_url and "localhost" in cfg.base_url or cfg.base_url and "127.0.0.1" in cfg.base_url):
            warnings.append(f"{name}_PROVIDER={cfg.provider} has no API key ({name}_API_KEY or {KEY_ENV[cfg.provider]}). Requests may be rejected.")
    if generator.enabled and judge.enabled and generator.describe() == judge.describe():
        warnings.append("The judge uses the same model as the generator. A model tends to rate its own output favorably; set JUDGE_MODEL to a different model family for an independent review.")

    data_path = Path(_env("DATA_PATH") or (ROOT / "data" / "sample_cases_synthetic.csv"))
    db_path = Path(_env("DB_PATH") or (ROOT / "data" / "triage.db"))
    return Settings(
        generator=generator,
        judge=judge,
        data_path=data_path,
        db_path=db_path,
        recorded_path=None if _env("RECORDED_REVIEW").lower() in ("off", "false", "0", "no")
        else Path(_env("RECORDED_REVIEW") or (ROOT / "data" / "recorded_review.json")),
        precompute=_env("PRECOMPUTE", "false").lower() in ("1", "true", "yes"),
        max_concurrency=max(1, int(_env("LLM_MAX_CONCURRENCY") or 3)),
        max_revisions=max(0, int(_env("JUDGE_MAX_REVISIONS") or 1)),
        judge_chat=_env("JUDGE_CHAT", "true").lower() not in ("0", "false", "no"),
        warnings=warnings,
    )
