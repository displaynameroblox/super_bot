import ipaddress
import json
import math
import os
import ssl
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
from time import monotonic, time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "superbot-phone"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_TIMEOUT_SECONDS = 600.0
PHONE_PROVIDER = "phone"
GEMINI_PROVIDER = "gemini"
OPENAI_PROVIDER = "openai"
OPENROUTER_PROVIDER = "openrouter"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GROQ_PROVIDER = "groq"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
CLOUD_PROVIDERS = {GEMINI_PROVIDER, OPENAI_PROVIDER, OPENROUTER_PROVIDER, GROQ_PROVIDER}
FAILOVER_PROVIDERS = {GROQ_PROVIDER, OPENROUTER_PROVIDER}
_cooldowns: dict[str, tuple[float, int]] = {}
_cooldown_lock = Lock()
INVALID_PROVIDER = "PHONE_MODEL_PROVIDER must be 'phone', 'gemini', 'openai', 'openrouter', or 'groq'."
OPENAI_BASE_URL = "https://api.openai.com/v1"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"

load_dotenv(Path.cwd() / ".env")  # installed copies read the .env in the folder you run from
load_dotenv(REPOSITORY_ROOT / ".env")


class PhoneModelError(RuntimeError):
    pass


class ProviderError(PhoneModelError):
    """Safe diagnostics derived from status, timing and known error metadata."""

    provider_name: str

    def __init__(self, status: int, retry_after: int | None, reason: str):
        self.status = status
        self.retry_after = retry_after
        timing = "" if retry_after is None else f" (retry after {retry_after}s)"
        super().__init__(f"{self.provider_name} returned HTTP {status}: {reason}{timing}.")


class OpenRouterError(ProviderError):
    provider_name = "OpenRouter"


class GroqError(ProviderError):
    provider_name = "Groq"


def _retry_after(response: httpx.Response) -> int | None:
    header = response.headers.get("Retry-After", "").strip()
    try:
        return (
            int(header) if header.isascii() and header.isdecimal() and len(header) <= 12
            else max(0, math.ceil(parsedate_to_datetime(header).timestamp() - time()))
        )
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _groq_error(response: httpx.Response) -> GroqError:
    status = response.status_code if not response.is_success else 502
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    reason = {
        400: "invalid request; check model and request parameters",
        401: "authentication failed; check GROQ_API_KEY",
        403: "access denied; check Groq model permissions",
        404: "model unavailable; check GROQ_MODEL_NAME",
        413: "request too large; shorten the conversation or input",
        429: "rate limit reached",
        503: "provider temporarily unavailable",
    }.get(status, "provider request failed")
    if status == 400 and code == "context_length_exceeded":
        reason = "model context limit exceeded; shorten the conversation or input"
    return GroqError(status, _retry_after(response) if status in {429, 503} else None, reason)


def _openrouter_error(response: httpx.Response, error: Any) -> OpenRouterError:
    status = response.status_code
    if response.is_success:
        code = error.get("code") if isinstance(error, dict) else None
        status = code if type(code) is int and 400 <= code <= 599 else 502

    # Provider text and nested metadata can echo request content and secrets.
    metadata = error.get("metadata") if isinstance(error, dict) else None
    source = metadata.get("limit_source") if isinstance(metadata, dict) else None
    metadata_headers = metadata.get("headers") if isinstance(metadata, dict) else None
    retry_after = None
    if status in {429, 503}:
        retry_after = _retry_after(response)
        reset = response.headers.get("X-RateLimit-Reset", "").strip()
        if not reset and isinstance(metadata_headers, dict):
            reset = metadata_headers.get("X-RateLimit-Reset", "")
        if status == 429 and isinstance(reset, str) and reset.isascii() and reset.isdecimal() and len(reset) <= 15:
            retry_after = max(retry_after or 0, math.ceil(int(reset) / 1000 - time()))

    reason = {
        400: "invalid request; check model compatibility and request parameters",
        401: "authentication failed; check OPENROUTER_API_KEY",
        403: "access denied; check key permissions and provider settings",
        404: "model or endpoint unavailable",
        429: "rate limit reached",
        503: "provider temporarily unavailable",
    }.get(status, "provider request failed")
    if status == 429:
        if source == "openrouter_free_tier_daily":
            reason = "OpenRouter daily free-tier quota reached"
            if retry_after is None:
                retry_after = math.ceil(86400 - time() % 86400)
        elif source == "openrouter_free_tier_per_minute":
            reason = "OpenRouter free-tier rate limit reached"
        elif source == "upstream_provider_shared_pool":
            reason = "upstream provider capacity limit reached"
    return OpenRouterError(status, retry_after, reason)


def _verify() -> ssl.SSLContext | bool:
    ca_file = os.getenv("PHONE_MODEL_CA_CERT", "").strip()
    if not ca_file:
        return True
    try:
        return ssl.create_default_context(cafile=ca_file)
    except (OSError, ssl.SSLError) as exc:
        raise PhoneModelError(
            f"Could not load PHONE_MODEL_CA_CERT {ca_file}: {exc}"
        ) from exc


def plaintext_link_warning() -> str | None:
    provider = os.getenv("PHONE_MODEL_PROVIDER", PHONE_PROVIDER).strip().lower()
    if provider in CLOUD_PROVIDERS:
        return None
    parsed_url = urlsplit(os.getenv("PHONE_MODEL_BASE_URL", "").strip())
    host = parsed_url.hostname
    if parsed_url.scheme != "http" or not host:
        return None
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if is_loopback:
        return None
    return (
        f"the link to the phone model ({parsed_url.netloc}) is unencrypted HTTP. "
        "Use an https:// PHONE_MODEL_BASE_URL (see Ai/termux/README.md) unless "
        "the network is fully trusted."
    )


def provider_description() -> str:
    provider = os.getenv("PHONE_MODEL_PROVIDER", PHONE_PROVIDER).strip().lower()
    _fallback_provider(provider)
    if provider == GROQ_PROVIDER:
        _groq_configuration()
        return "GroqCloud; messages are sent to Groq"
    if provider == PHONE_PROVIDER:
        model = os.getenv("PHONE_MODEL_NAME", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        return f"Phone-hosted model ({model})"
    if provider == OPENAI_PROVIDER:
        _, _, model, _ = _openai_configuration()
        return f"OpenAI cloud model ({model}); messages are sent to OpenAI"
    if provider == OPENROUTER_PROVIDER:
        _, _, model, _ = _openrouter_configuration()
        return f"OpenRouter cloud model ({model}); messages are sent via OpenRouter"
    if provider == GEMINI_PROVIDER:
        if not os.getenv("GEMINI_API_KEY", "").strip():
            raise PhoneModelError(
                "Set GEMINI_API_KEY in the server's environment or .env file "
                "to use the Gemini provider."
            )
        model = os.getenv("GEMINI_MODEL_NAME", DEFAULT_GEMINI_MODEL).strip()
        if not model:
            raise PhoneModelError("GEMINI_MODEL_NAME cannot be empty.")
        return f"Google Gemini cloud model ({model}); messages are sent to Google"
    raise PhoneModelError(INVALID_PROVIDER)


def probe() -> None:
    """Verify that the configured inference provider and model are available."""
    provider = os.getenv("PHONE_MODEL_PROVIDER", PHONE_PROVIDER).strip().lower()
    if provider == GEMINI_PROVIDER:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        model = os.getenv("GEMINI_MODEL_NAME", DEFAULT_GEMINI_MODEL).strip()
        if not api_key:
            raise PhoneModelError(
                "Set GEMINI_API_KEY in the server's environment or .env file "
                "to use the Gemini provider."
            )
        if not model:
            raise PhoneModelError("GEMINI_MODEL_NAME cannot be empty.")
        url = f"{GEMINI_API_URL}/{quote(model, safe='')}"
        headers = {"x-goog-api-key": api_key}
        name = "Gemini"
    elif provider == OPENAI_PROVIDER:
        base_url, api_key, model, timeout_seconds = _openai_configuration()
        url = f"{base_url}/models/{quote(model, safe='')}"
        headers = {"Authorization": f"Bearer {api_key}"}
        name = "OpenAI"
    elif provider == OPENROUTER_PROVIDER:
        base_url, api_key, model, timeout_seconds = _openrouter_configuration()
        url = f"{base_url}/key"
        headers = {"Authorization": f"Bearer {api_key}"}
        name = "OpenRouter"
    elif provider == GROQ_PROVIDER:
        base_url, api_key, model, timeout_seconds = _groq_configuration()
        url = f"{base_url}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        name = "Groq"
    elif provider == PHONE_PROVIDER:
        base_url, api_key, model, timeout_seconds = _configuration()
        url = f"{base_url}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        name = "Phone model"
    else:
        raise PhoneModelError(INVALID_PROVIDER)

    timeout = httpx.Timeout(
        connect=10.0,
        read=30.0 if provider == GEMINI_PROVIDER else min(timeout_seconds, 30.0),
        write=10.0,
        pool=10.0,
    )
    try:
        with httpx.Client(
            timeout=timeout, trust_env=False,
            verify=True if provider in CLOUD_PROVIDERS else _verify(),
        ) as client:
            response = client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise PhoneModelError(
            f"{name} did not respond to its readiness check."
        ) from (None if provider == GROQ_PROVIDER else exc)
    except httpx.HTTPError as exc:
        raise PhoneModelError(
            f"Could not reach the {name.lower()} for its readiness check."
        ) from (None if provider == GROQ_PROVIDER else exc)

    if provider == GROQ_PROVIDER and not response.is_success:
        raise _groq_error(response)
    if response.status_code in {401, 403}:
        key_name = {GEMINI_PROVIDER: "GEMINI_API_KEY", OPENAI_PROVIDER: "OPENAI_API_KEY",
                    OPENROUTER_PROVIDER: "OPENROUTER_API_KEY", PHONE_PROVIDER: "TEST_PHONE_API_KEY"}[provider]
        raise PhoneModelError(f"{name} rejected the API key; check {key_name}.")
    if not response.is_success:
        raise PhoneModelError(f"{name} readiness check returned HTTP {response.status_code}.")
    if provider in {PHONE_PROVIDER, GROQ_PROVIDER}:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise PhoneModelError(f"{name} readiness check returned invalid JSON.") from exc
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list) or not any(
            isinstance(item, dict) and item.get("id") == model for item in models
        ):
            raise PhoneModelError(
                "Groq readiness check did not list the configured model. Check GROQ_MODEL_NAME."
                if provider == GROQ_PROVIDER
                else f"Phone model readiness check did not list configured model {model!r}."
            )


def _openai_configuration() -> tuple[str, str, str, float]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL_NAME", "").strip()
    if not api_key:
        raise PhoneModelError("Set OPENAI_API_KEY in the server's environment or .env file.")
    if not model:
        raise PhoneModelError("Set OPENAI_MODEL_NAME to an OpenAI model available to your API key.")
    try:
        timeout = float(os.getenv("PHONE_MODEL_TIMEOUT_SECONDS", "600"))
    except ValueError as exc:
        raise PhoneModelError("PHONE_MODEL_TIMEOUT_SECONDS must be a number.") from exc
    if not 10 <= timeout <= 1800:
        raise PhoneModelError("PHONE_MODEL_TIMEOUT_SECONDS must be between 10 and 1800.")
    return OPENAI_BASE_URL, api_key, model, timeout


def _groq_configuration() -> tuple[str, str, str, float]:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    model = os.getenv("GROQ_MODEL_NAME", DEFAULT_GROQ_MODEL).strip()
    if not api_key:
        raise PhoneModelError("Set GROQ_API_KEY in the server's environment or .env file.")
    if not model:
        raise PhoneModelError("Set GROQ_MODEL_NAME to a Groq chat model ID.")
    timeout = _groq_number("GROQ_TIMEOUT_SECONDS", 45, 5, 180)
    _groq_number("GROQ_MAX_COMPLETION_TOKENS", 2048, 1, 65536)
    return GROQ_BASE_URL, api_key, model, timeout


def _groq_number(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        if minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    raise PhoneModelError(f"{name} must be an integer from {minimum} to {maximum}.")


def _openrouter_configuration() -> tuple[str, str, str, float]:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = os.getenv("OPENROUTER_MODEL_NAME", "").strip()
    if not api_key:
        raise PhoneModelError("Set OPENROUTER_API_KEY in the environment or .env file.")
    if not model:
        raise PhoneModelError("Set OPENROUTER_MODEL_NAME to an OpenRouter model ID.")
    try:
        timeout = float(os.getenv("PHONE_MODEL_TIMEOUT_SECONDS", "600"))
    except ValueError as exc:
        raise PhoneModelError("PHONE_MODEL_TIMEOUT_SECONDS must be a number.") from exc
    if not 10 <= timeout <= 1800:
        raise PhoneModelError("PHONE_MODEL_TIMEOUT_SECONDS must be between 10 and 1800.")
    return OPENROUTER_BASE_URL, api_key, model, timeout


def _configuration() -> tuple[str, str, str, float]:
    base_url = os.getenv("PHONE_MODEL_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("TEST_PHONE_API_KEY", "").strip()
    model = os.getenv("PHONE_MODEL_NAME", DEFAULT_MODEL).strip()
    timeout_value = os.getenv("PHONE_MODEL_TIMEOUT_SECONDS", "600")

    if not base_url:
        raise PhoneModelError(
            "Set PHONE_MODEL_BASE_URL in the repository .env file "
            "(for example, http://192.168.1.42:8080/v1)."
        )
    parsed_url = urlsplit(base_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.path.rstrip("/") != "/v1"
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise PhoneModelError(
            "PHONE_MODEL_BASE_URL must be an http(s) URL ending in /v1."
        )
    if not api_key:
        raise PhoneModelError(
            "Set TEST_PHONE_API_KEY in the repository .env file."
        )
    if not model:
        raise PhoneModelError("PHONE_MODEL_NAME cannot be empty.")
    try:
        timeout = float(timeout_value)
    except ValueError as exc:
        raise PhoneModelError(
            "PHONE_MODEL_TIMEOUT_SECONDS must be a number."
        ) from exc
    if not 10 <= timeout <= 1800:
        raise PhoneModelError(
            "PHONE_MODEL_TIMEOUT_SECONDS must be between 10 and 1800."
        )
    return base_url, api_key, model, timeout


def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    supported_keys = {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "description",
        "format",
        "nullable",
    }
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in supported_keys:
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {
                name: _gemini_schema(property_schema)
                for name, property_schema in value.items()
                if isinstance(name, str) and isinstance(property_schema, dict)
            }
        elif key == "items" and isinstance(value, dict):
            result[key] = _gemini_schema(value)
        elif key == "type" and isinstance(value, str):
            result[key] = value.upper()
        elif isinstance(value, (str, bool, list)):
            result[key] = value
    return result


def _gemini_chat(
    messages: list[dict[str, str]],
    response_schema: dict[str, Any] | None,
    model: str | None,
    max_output_tokens: int | None,
) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise PhoneModelError(
            "Set GEMINI_API_KEY in the server's environment or .env file "
            "to use the Gemini provider."
        )
    model_name = model or os.getenv("GEMINI_MODEL_NAME", DEFAULT_GEMINI_MODEL).strip()
    if not model_name:
        raise PhoneModelError("GEMINI_MODEL_NAME cannot be empty.")

    system_parts = [
        {"text": message["content"]}
        for message in messages
        if message.get("role") == "system"
    ]
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        if role not in {"user", "assistant"}:
            raise PhoneModelError(f"Unsupported Gemini message role: {role!r}.")
        contents.append(
            {
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": message["content"]}],
            }
        )

    payload: dict[str, Any] = {"contents": contents}
    if system_parts:
        payload["systemInstruction"] = {"parts": system_parts}
    generation_config: dict[str, Any] = {"maxOutputTokens": max_output_tokens or 512}
    if response_schema is not None:
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = _gemini_schema(response_schema)
    payload["generationConfig"] = generation_config

    try:
        timeout_seconds = float(os.getenv("PHONE_MODEL_TIMEOUT_SECONDS", "600"))
    except ValueError as exc:
        raise PhoneModelError("PHONE_MODEL_TIMEOUT_SECONDS must be a number.") from exc
    if not 10 <= timeout_seconds <= 1800:
        raise PhoneModelError(
            "PHONE_MODEL_TIMEOUT_SECONDS must be between 10 and 1800."
        )
    timeout = httpx.Timeout(
        connect=10.0,
        read=timeout_seconds,
        write=30.0,
        pool=10.0,
    )
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.post(
                f"{GEMINI_API_URL}/{quote(model_name, safe='')}:generateContent",
                headers={"x-goog-api-key": api_key},
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise PhoneModelError(
            f"Gemini request timed out after {timeout_seconds:g} seconds."
        ) from exc
    except httpx.HTTPError as exc:
        raise PhoneModelError("Could not reach the Google Gemini API.") from exc

    if response.status_code in {401, 403}:
        raise PhoneModelError("Gemini rejected the API key; check GEMINI_API_KEY.")
    if not response.is_success:
        detail = response.text.strip().replace("\n", " ")[:400]
        raise PhoneModelError(
            f"Gemini returned HTTP {response.status_code}"
            f"{': ' + detail if detail else ''}."
        )
    try:
        result = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise PhoneModelError("Gemini returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise PhoneModelError("Gemini returned an unexpected response.")
    candidates = result.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else None
    content = candidate.get("content") if isinstance(candidate, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        finish_reason = (
            candidate.get("finishReason") if isinstance(candidate, dict) else None
        )
        finish_note = f" (finishReason={finish_reason})" if finish_reason else ""
        raise PhoneModelError(f"Gemini returned no candidate{finish_note}.")
    text = "".join(
        part["text"]
        for part in parts
        if (
            isinstance(part, dict)
            and part.get("thought") is not True
            and isinstance(part.get("text"), str)
        )
    ).strip()
    if not text:
        raise PhoneModelError("Gemini returned no visible reply.")
    return text


def _apply_openrouter_options(payload: dict[str, Any]) -> None:
    temperature = os.getenv("OPENROUTER_TEMPERATURE", "").strip()
    max_tokens = os.getenv("OPENROUTER_MAX_TOKENS", "").strip()
    reasoning = os.getenv("OPENROUTER_REASONING_ENABLED", "").strip().lower()
    if temperature:
        try:
            value = float(temperature)
        except ValueError:
            value = float("nan")
        if not 0 <= value <= 2:
            raise PhoneModelError("OPENROUTER_TEMPERATURE must be between 0 and 2.")
        payload["temperature"] = value
    if max_tokens:
        if not max_tokens.isascii() or not max_tokens.isdecimal() or len(max_tokens) > 9 or int(max_tokens) < 1:
            raise PhoneModelError("OPENROUTER_MAX_TOKENS must be a positive integer.")
        payload["max_tokens"] = min(int(max_tokens), payload["max_tokens"])
    if reasoning:
        if reasoning not in {"true", "false"}:
            raise PhoneModelError("OPENROUTER_REASONING_ENABLED must be true or false.")
        payload["reasoning"] = {"enabled": reasoning == "true"}


def chat(
    messages: list[dict[str, str]],
    response_schema: dict[str, Any] | None = None,
    model: str | None = None,
    *,
    max_output_tokens: int | None = None,
) -> str:
    provider = os.getenv("PHONE_MODEL_PROVIDER", PHONE_PROVIDER).strip().lower()
    fallback = _fallback_provider(provider)
    use_cooldown = provider == GROQ_PROVIDER or fallback is not None
    try:
        return _chat_once(provider, messages, response_schema, model, max_output_tokens, use_cooldown)
    except ProviderError as exc:
        if fallback is None or exc.status not in {429, 503}:
            raise
    return _chat_once(fallback, messages, response_schema, None, max_output_tokens, True)


def _fallback_provider(primary: str) -> str | None:
    fallback = os.getenv("PHONE_MODEL_FALLBACK_PROVIDER", "").strip().lower()
    if not fallback:
        return None
    if {primary, fallback} != FAILOVER_PROVIDERS:
        raise PhoneModelError("PHONE_MODEL_FALLBACK_PROVIDER must pair Groq and OpenRouter.")
    # Check both credentials and models at startup, before failover is needed.
    _groq_configuration()
    _openrouter_configuration()
    return fallback


def _chat_once(
    provider: str,
    messages: list[dict[str, str]],
    response_schema: dict[str, Any] | None,
    model: str | None,
    max_output_tokens: int | None,
    use_cooldown: bool,
) -> str:
    if use_cooldown:
        with _cooldown_lock:
            deadline, status = _cooldowns.get(provider, (0.0, 429))
            remaining = math.ceil(deadline - monotonic())
        if remaining > 0:
            error_class = GroqError if provider == GROQ_PROVIDER else OpenRouterError
            raise error_class(status, remaining, "provider cooldown active")
    try:
        return _provider_chat(provider, messages, response_schema, model, max_output_tokens)
    except ProviderError as exc:
        if use_cooldown and exc.status in {429, 503}:
            with _cooldown_lock:
                deadline = monotonic() + max(1, exc.retry_after if exc.retry_after is not None else 60)
                if deadline > _cooldowns.get(provider, (0.0, 429))[0]:
                    _cooldowns[provider] = (deadline, exc.status)
        raise


def _provider_chat(
    provider: str,
    messages: list[dict[str, str]],
    response_schema: dict[str, Any] | None,
    model: str | None,
    max_output_tokens: int | None,
) -> str:
    if provider == GEMINI_PROVIDER:
        return _gemini_chat(messages, response_schema, model, max_output_tokens)
    if provider == OPENAI_PROVIDER:
        name, key_name = "OpenAI", "OPENAI_API_KEY"
        configuration = _openai_configuration
    elif provider == GROQ_PROVIDER:
        name, key_name = "Groq", "GROQ_API_KEY"
        configuration = _groq_configuration
    elif provider == OPENROUTER_PROVIDER:
        name, key_name = "OpenRouter", "OPENROUTER_API_KEY"
        configuration = _openrouter_configuration
    elif provider == PHONE_PROVIDER:
        name, key_name = "Phone model", "TEST_PHONE_API_KEY"
        configuration = _configuration
    else:
        raise PhoneModelError(INVALID_PROVIDER)

    base_url, api_key, configured_model, timeout_seconds = configuration()
    payload: dict[str, Any] = {
        "model": model or configured_model,
        "messages": messages,
        "max_tokens": max_output_tokens or 512,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if provider in CLOUD_PROVIDERS:
        payload.pop("chat_template_kwargs")
    if provider == OPENAI_PROVIDER:
        payload.pop("max_tokens")
        payload["max_completion_tokens"] = max_output_tokens or 4096
    if provider == GROQ_PROVIDER:
        payload.pop("max_tokens")
        cap = _groq_number("GROQ_MAX_COMPLETION_TOKENS", 2048, 1, 65536)
        payload["max_completion_tokens"] = min(max_output_tokens or cap, cap)
        if payload["model"] in {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}:
            payload["reasoning_effort"] = "low"
    if provider == OPENROUTER_PROVIDER:
        payload["max_tokens"] = max_output_tokens or 4096
        # Plain chat can route even when an endpoint ignores optional tuning.
        # Structured callers still require support for their response schema.
        if response_schema is not None:
            payload["provider"] = {"require_parameters": True}
        _apply_openrouter_options(payload)
    if response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "super_bot_response",
                "schema": response_schema,
            },
        }

    timeout = httpx.Timeout(
        connect=10.0,
        read=timeout_seconds,
        write=30.0,
        pool=10.0,
    )
    try:
        with httpx.Client(
            timeout=timeout, trust_env=False,
            verify=True if provider in CLOUD_PROVIDERS else _verify(),
        ) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise PhoneModelError(
            f"{name} request timed out after {timeout_seconds:g} seconds."
        ) from (None if provider == GROQ_PROVIDER else exc)
    except httpx.HTTPError as exc:
        raise PhoneModelError(
            f"Could not reach {name} at {base_url}."
        ) from (None if provider == GROQ_PROVIDER else exc)

    if provider == GROQ_PROVIDER and not response.is_success:
        raise _groq_error(response)
    if provider == OPENROUTER_PROVIDER and not response.is_success:
        try:
            body = response.json()
        except ValueError:
            body = None
        raise _openrouter_error(response, body.get("error") if isinstance(body, dict) else None)

    if response.status_code in {401, 403}:
        raise PhoneModelError(
            f"{name} rejected the API key; check {key_name}."
        )
    if not response.is_success:
        detail = "" if provider in CLOUD_PROVIDERS else response.text.strip().replace("\n", " ")[:400]
        raise PhoneModelError(
            f"{name} returned HTTP {response.status_code}"
            f"{': ' + detail if detail else ''}."
        )
    try:
        result = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise PhoneModelError(f"{name} returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise PhoneModelError(f"{name} returned an unexpected response.")
    if provider == OPENROUTER_PROVIDER and isinstance(result.get("error"), dict):
        raise _openrouter_error(response, result["error"])
    if provider == GROQ_PROVIDER and isinstance(result.get("error"), dict):
        raise _groq_error(response)
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise PhoneModelError(f"{name} response did not contain a choice.")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise PhoneModelError(f"{name} response did not contain a message.")
    if provider == GROQ_PROVIDER and choice.get("finish_reason") == "length":
        raise PhoneModelError("Groq reached the completion token limit. Shorten the request or review the completion budget.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        finish_reason = choice.get("finish_reason")
        reasoning = message.get("reasoning_content")
        reasoning_note = (
            f"; model generated {len(reasoning)} reasoning characters but no visible content"
            if isinstance(reasoning, str) and reasoning
            else ""
        )
        finish_note = (
            f" (finish_reason={finish_reason})"
            if finish_reason in ("length", "stop", "tool_calls", "content_filter")
            else ""
        )
        raise PhoneModelError(
            f"{name} returned no visible reply{finish_note}{reasoning_note}."
        )
    return content.strip()
