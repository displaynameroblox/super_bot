import ipaddress
import json
import os
import ssl
from pathlib import Path
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
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"

load_dotenv(Path.cwd() / ".env")  # installed copies read the .env in the folder you run from
load_dotenv(REPOSITORY_ROOT / ".env")


class PhoneModelError(RuntimeError):
    pass


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
    if provider == GEMINI_PROVIDER:
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
    if provider == PHONE_PROVIDER:
        model = os.getenv("PHONE_MODEL_NAME", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        return f"Phone-hosted model ({model})"
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
    raise PhoneModelError("PHONE_MODEL_PROVIDER must be 'phone' or 'gemini'.")


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
    generation_config: dict[str, Any] = {"maxOutputTokens": 512}
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


def chat(
    messages: list[dict[str, str]],
    response_schema: dict[str, Any] | None = None,
    model: str | None = None,
) -> str:
    provider = os.getenv("PHONE_MODEL_PROVIDER", PHONE_PROVIDER).strip().lower()
    if provider == GEMINI_PROVIDER:
        return _gemini_chat(messages, response_schema, model)
    if provider != PHONE_PROVIDER:
        raise PhoneModelError("PHONE_MODEL_PROVIDER must be 'phone' or 'gemini'.")

    base_url, api_key, configured_model, timeout_seconds = _configuration()
    payload: dict[str, Any] = {
        "model": model or configured_model,
        "messages": messages,
        "max_tokens": 512,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
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
        with httpx.Client(timeout=timeout, trust_env=False, verify=_verify()) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise PhoneModelError(
            f"Phone model request timed out after {timeout_seconds:g} seconds."
        ) from exc
    except httpx.HTTPError as exc:
        raise PhoneModelError(
            f"Could not reach the phone model at {base_url}: {exc}"
        ) from exc

    if response.status_code in {401, 403}:
        raise PhoneModelError(
            "Phone model rejected the API key; check TEST_PHONE_API_KEY."
        )
    if not response.is_success:
        detail = response.text.strip().replace("\n", " ")[:400]
        raise PhoneModelError(
            f"Phone model returned HTTP {response.status_code}"
            f"{': ' + detail if detail else ''}."
        )
    try:
        result = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise PhoneModelError("Phone model returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise PhoneModelError("Phone model returned an unexpected response.")
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise PhoneModelError("Phone model response did not contain a choice.")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise PhoneModelError("Phone model response did not contain a message.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        finish_reason = choice.get("finish_reason")
        reasoning = message.get("reasoning_content")
        reasoning_note = (
            f"; model generated {len(reasoning)} reasoning characters but no visible content"
            if isinstance(reasoning, str) and reasoning
            else ""
        )
        finish_note = f" (finish_reason={finish_reason})" if finish_reason else ""
        raise PhoneModelError(
            f"Phone model returned no visible reply{finish_note}{reasoning_note}."
        )
    return content.strip()
