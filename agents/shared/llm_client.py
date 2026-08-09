"""The only module in the project that talks to an LLM.

No agent may import litellm directly. Everything goes through ``complete()``,
which gives us four things a free tier makes mandatory:

* **Disk cache.** Keyed on ``sha256(model + prompt)``. The service contract and
  the carrier advisory never change, so after the first run those calls cost
  nothing. Dev iterations become free.
* **Modes.** ``mock`` never touches the network, ``cache`` refuses to, ``live``
  will. Wire workflows in ``mock``; demo from ``cache``.
* **One repair retry.** Free models return malformed JSON often enough to
  matter. A failed request still burns quota, so retries are bounded at one.
* **Provider choice.** The ``LLM_MODEL`` prefix selects the provider and its
  key, so moving between Gemini and OpenRouter is config, not code.

Configure with two variables in ``.env``::

    LLM_MODEL=gemini/gemini-2.0-flash      GEMINI_API_KEY=...
    LLM_MODEL=openrouter/<vendor>/<model>  OPENROUTER_API_KEY=...

Note that the cache key includes the model, so changing provider starts from a
cold cache; the first ``live`` run re-warms it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, Type, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

CACHE_DIR = Path(__file__).parent / "cache"
FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"

DEFAULT_MODEL = "openrouter/meta-llama/llama-3.3-70b-instruct:free"

# Model prefix -> the env var holding that provider's key. litellm routes on the
# prefix, so the key has to match the provider or the call comes back 401.
# Adding a provider is a line here plus a key in .env; no agent changes.
PROVIDER_KEYS: dict[str, str] = {
    "gemini/": "GEMINI_API_KEY",
    "openrouter/": "OPENROUTER_API_KEY",
}


class LlmMode:
    MOCK = "mock"
    CACHE = "cache"
    LIVE = "live"


class LlmError(RuntimeError):
    pass


class QuotaExhausted(LlmError):
    """Raised on a 429 so callers can fall back rather than hammer the API."""


def _mode() -> str:
    return os.getenv("LLM_MODE", LlmMode.MOCK).strip().lower()


def _model() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_MODEL).strip()


def _api_key_for(model: str) -> str:
    """The API key for whichever provider ``model`` names.

    Raises:
        LlmError: If the provider is unrecognised, or its key is not configured.
    """
    for prefix, env_var in PROVIDER_KEYS.items():
        if model.startswith(prefix):
            key = os.getenv(env_var)
            if not key:
                raise LlmError(
                    f"LLM_MODEL={model} needs {env_var}, which is not set. "
                    f"Add it to .env, or run with LLM_MODE=mock."
                )
            return key

    raise LlmError(
        f"Unrecognised provider for LLM_MODEL={model}. Prefix the model with one "
        f"of: {', '.join(sorted(PROVIDER_KEYS))}"
    )


def _cache_key(model: str, prompt: str, schema_name: str) -> str:
    digest = hashlib.sha256(f"{model}\x00{schema_name}\x00{prompt}".encode()).hexdigest()
    return digest[:32]


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _read_cache(key: str) -> Optional[str]:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))["response"]
    except (json.JSONDecodeError, KeyError, OSError):
        log.warning("corrupt cache entry %s, ignoring", key)
        return None


def _write_cache(key: str, model: str, prompt: str, response: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(key).write_text(
        json.dumps(
            {"model": model, "prompt": prompt, "response": response, "cached_at": time.time()},
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_fixture(name: str) -> Optional[str]:
    path = FIXTURE_DIR / f"{name}.json"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _schema_block(schema: Type[BaseModel]) -> str:
    """The exact output contract, generated from the model that will validate it.

    Prose prompts describe intent; this describes shape. Deriving it from the
    Pydantic class means the two cannot drift apart - a prompt that asks for a
    field the schema forbids is no longer possible.
    """
    return (
        "Reply with ONE JSON object and nothing else - no prose, no markdown "
        "fences. Omit optional fields you cannot fill rather than inventing "
        "them, and do not add fields that are not listed. It must validate "
        "against this JSON schema:\n"
        + json.dumps(schema.model_json_schema(), indent=2)
    )


def _extract_json(raw: str) -> str:
    """Pull a JSON object out of a model response.

    Free models wrap JSON in prose or fences far more often than paid ones, so
    this is load-bearing rather than defensive.
    """
    text = raw.strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _call_llm(prompt: str, system: str, model: str, max_tokens: int) -> str:
    """The only outbound network call in the codebase.

    Provider is chosen by the ``LLM_MODEL`` prefix, so switching between
    OpenRouter and Gemini is a config change rather than a code change.
    """
    import litellm  # imported lazily so mock mode needs no API key

    api_key = _api_key_for(model)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=0.0,  # determinism matters more than flair here
        )
    except Exception as exc:  # noqa: BLE001 - litellm raises provider-specific types
        text = str(exc)
        if "429" in text or "rate limit" in text.lower():
            raise QuotaExhausted(
                f"Rate limit hit on {model}. Free tiers are metered and failed "
                f"requests still count against them. Re-run with LLM_MODE=cache "
                f"to serve from disk, or switch LLM_MODEL to another provider."
            ) from exc
        raise LlmError(f"LLM call failed: {text}") from exc

    return response.choices[0].message.content or ""


def complete(
    prompt: str,
    schema: Type[T],
    *,
    system: str = "",
    fixture: Optional[str] = None,
    max_tokens: int = 4000,
) -> T:
    """Return a validated ``schema`` instance from the model.

    Args:
        prompt: The user prompt.
        schema: Pydantic model the response must validate against.
        system: Optional system prompt.
        fixture: Fixture basename used in ``mock`` mode and as the last-resort
            fallback if a live call returns unparseable output.
        max_tokens: Response cap. Generous because reasoning models spend part
            of this budget on thinking tokens before emitting any JSON, and a
            truncated object costs a whole extra repair call to recover.

    Raises:
        LlmError: On a cache miss in ``cache`` mode, a missing fixture in
            ``mock`` mode, or output that will not validate after one repair.
        QuotaExhausted: On a 429 from the provider.
    """
    mode = _mode()
    model = _model()

    # The shape contract travels with every prompt, so the cache key covers it too.
    full_prompt = f"{prompt}\n\n{_schema_block(schema)}"
    key = _cache_key(model, full_prompt, schema.__name__)

    # 1. Mock mode: fixtures only, no network, no cache.
    if mode == LlmMode.MOCK:
        if not fixture:
            raise LlmError(
                f"LLM_MODE=mock but no fixture given for {schema.__name__}. "
                "Pass fixture= or switch to LLM_MODE=live."
            )
        raw = _read_fixture(fixture)
        if raw is None:
            raise LlmError(
                f"LLM_MODE=mock but fixture '{fixture}.json' is missing from "
                f"{FIXTURE_DIR}."
            )
        return schema.model_validate_json(_extract_json(raw))

    # 2. Cache hit short-circuits every mode. This is what keeps us solvent.
    cached = _read_cache(key)
    if cached is not None:
        log.info("llm cache hit  %s  %s", schema.__name__, key)
        try:
            return schema.model_validate_json(_extract_json(cached))
        except ValidationError:
            log.warning("cached payload no longer matches %s, refetching", schema.__name__)

    if mode == LlmMode.CACHE:
        raise LlmError(
            f"LLM_MODE=cache and no cached response for {schema.__name__} ({key}). "
            "Run once with LLM_MODE=live to warm the cache."
        )

    # 3. Live call.
    log.info("llm live call  %s  model=%s", schema.__name__, model)
    raw = _call_llm(full_prompt, system, model, max_tokens)

    try:
        parsed = schema.model_validate_json(_extract_json(raw))
        _write_cache(key, model, full_prompt, raw)
        return parsed
    except ValidationError as exc:
        # Bound to a new name: Python unbinds the `as` target on block exit.
        first_error = str(exc)
        log.warning(
            "%s failed validation, attempting one repair: %s",
            schema.__name__,
            first_error.replace("\n", " ")[:300],
        )

    # 4. Exactly one repair attempt. Bounded because failures burn quota too.
    # Feeding the errors back is what makes the retry worth its quota.
    repair_prompt = (
        f"{full_prompt}\n\n"
        f"Your previous reply did not validate. Fix exactly these errors and "
        f"return the corrected JSON object only:\n{first_error}"
    )
    repaired = _call_llm(repair_prompt, system, model, max_tokens)

    try:
        parsed = schema.model_validate_json(_extract_json(repaired))
        _write_cache(key, model, full_prompt, repaired)
        return parsed
    except ValidationError as exc:
        if fixture:
            fallback = _read_fixture(fixture)
            if fallback is not None:
                log.error("%s unparseable after repair, falling back to fixture", schema.__name__)
                return schema.model_validate_json(_extract_json(fallback))
        raise LlmError(
            f"{schema.__name__} could not be parsed after one repair attempt.\n"
            f"Raw response: {repaired[:500]}"
        ) from exc


def cache_stats() -> dict[str, int | str]:
    """Reported at worker startup so quota burn is never a surprise."""
    entries = list(CACHE_DIR.glob("*.json")) if CACHE_DIR.exists() else []
    return {
        "mode": _mode(),
        "model": _model(),
        "cached_responses": len(entries),
        "fixtures": len(list(FIXTURE_DIR.glob("*.json"))) if FIXTURE_DIR.exists() else 0,
    }
