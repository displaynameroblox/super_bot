import json
from unittest.mock import patch

import httpx
import pytest

from Ai import phone_model


MESSAGES = [{"role": "user", "content": "Hi Miso"}]
MODEL = "openai/gpt-oss-120b"
REPLY = {"choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}]}


@pytest.fixture(autouse=True)
def groq(monkeypatch):
    monkeypatch.setenv("PHONE_MODEL_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "fake-key")
    monkeypatch.delenv("GROQ_MODEL_NAME", raising=False)
    monkeypatch.delenv("GROQ_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("GROQ_MAX_COMPLETION_TOKENS", raising=False)
    monkeypatch.delenv("PHONE_MODEL_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setattr(phone_model, "_cooldowns", {})


@pytest.mark.parametrize("model", [None, "openai/gpt-oss-20b"])
def test_probe_and_structured_chat(monkeypatch, model):
    if model:
        monkeypatch.setenv("GROQ_MODEL_NAME", model)
    monkeypatch.setenv("GROQ_TIMEOUT_SECONDS", "21")
    schema = {"type": "object"}

    def handler(request):
        assert request.url.host == "api.groq.com"
        assert request.headers["authorization"] == "Bearer fake-key"
        assert request.extensions["timeout"]["read"] == 21
        if request.method == "GET":
            assert request.url.path == "/openai/v1/models"
            return httpx.Response(200, json={"data": [{"id": model or MODEL}]})
        assert request.url.path == "/openai/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload == {
            "model": model or MODEL,
            "messages": MESSAGES,
            "stream": False,
            "max_completion_tokens": 2048,
            "reasoning_effort": "low",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "super_bot_response", "schema": schema},
            },
        }
        return httpx.Response(200, json=REPLY)

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=21)
    with patch.object(phone_model.httpx, "Client", return_value=client):
        phone_model.probe()
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=21)
    with patch.object(phone_model.httpx, "Client", return_value=client):
        assert phone_model.chat(MESSAGES, schema, max_output_tokens=6000) == "Hello"


@pytest.mark.parametrize("primary", ["groq", "openrouter"])
def test_fallback_cooldown_and_recovery(monkeypatch, primary):
    fallback = "openrouter" if primary == "groq" else "groq"
    monkeypatch.setenv("PHONE_MODEL_PROVIDER", primary)
    monkeypatch.setenv("PHONE_MODEL_FALLBACK_PROVIDER", fallback)
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter-key")
    monkeypatch.setenv("OPENROUTER_MODEL_NAME", "fallback/model")
    now = [100.0]
    monkeypatch.setattr(phone_model, "monotonic", lambda: now[0])
    calls = []
    primary_host = "api.groq.com" if primary == "groq" else "openrouter.ai"
    fallback_host = "openrouter.ai" if primary == "groq" else "api.groq.com"
    fallback_model = "fallback/model" if fallback == "openrouter" else MODEL

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append((request.url.host, model))
        if request.url.host == primary_host and now[0] < 280:
            return httpx.Response(429, headers={"Retry-After": "180"})
        return httpx.Response(200, json=REPLY)

    client_class = httpx.Client
    with patch.object(phone_model.httpx, "Client", side_effect=lambda **kwargs: client_class(
        transport=httpx.MockTransport(handler), **kwargs
    )):
        assert phone_model.chat(MESSAGES, model="override/model") == "Hello"
        assert phone_model.chat(MESSAGES) == "Hello"
        now[0] = 280.0
        assert phone_model.chat(MESSAGES, model="override/model") == "Hello"
    assert calls == [
        (primary_host, "override/model"),
        (fallback_host, fallback_model),
        (fallback_host, fallback_model),
        (primary_host, "override/model"),
    ]


@pytest.mark.parametrize(("response", "message"), [
    (httpx.Response(401, json={"error": {"message": "fake-key"}}), "GROQ_API_KEY"),
    (httpx.Response(400, json={"error": {"code": "context_length_exceeded"}}), "context limit"),
    (httpx.Response(200, json={"choices": [
        {"message": {"content": "incomplete"}, "finish_reason": "length"},
    ]}), "completion token limit"),
])
def test_safe_errors(response, message):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: response))
    with patch.object(phone_model.httpx, "Client", return_value=client) as factory:
        with pytest.raises(phone_model.PhoneModelError, match=message) as error:
            phone_model.chat(MESSAGES)
    assert "fake-key" not in str(error.value)
    assert factory.call_args.kwargs["timeout"].read == 45
    assert factory.call_count == 1



def test_groq_bypasses_managed_local_model(tmp_path, monkeypatch):
    from Ai import managed_model

    monkeypatch.setattr(managed_model, "read_json", lambda path: {"path": "local.gguf"})
    assert managed_model.selected_model_path(tmp_path) is None


def test_timeout_does_not_trigger_fallback(monkeypatch):
    monkeypatch.setenv("PHONE_MODEL_FALLBACK_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter-key")
    monkeypatch.setenv("OPENROUTER_MODEL_NAME", "fallback/model")

    def handler(request):
        raise httpx.ReadTimeout("fake-key", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with patch.object(phone_model.httpx, "Client", return_value=client) as factory:
        with pytest.raises(phone_model.PhoneModelError, match="timed out after 45") as error:
            phone_model.chat(MESSAGES)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    assert factory.call_count == 1
