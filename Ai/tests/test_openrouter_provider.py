from unittest.mock import patch

import httpx
import pytest

from Ai import phone_model


def test_openrouter_routing_probe_cap_and_redaction(monkeypatch):
    monkeypatch.setenv("PHONE_MODEL_PROVIDER", "openrouter")
    for name in ("OPENROUTER_TEMPERATURE", "OPENROUTER_MAX_TOKENS", "OPENROUTER_REASONING_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "example-key")
    monkeypatch.setenv("OPENROUTER_MODEL_NAME", "org/test-model")
    monkeypatch.setenv("PHONE_MODEL_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("PHONE_MODEL_BASE_URL", "http://wrong-endpoint:8080/v1")
    monkeypatch.setenv("PHONE_MODEL_CA_CERT", "/wrong-cert")
    with patch.object(phone_model.httpx, "Client") as factory:
        client = factory.return_value.__enter__.return_value
        client.get.return_value = httpx.Response(200, json={"data": {}})
        phone_model.probe()
        assert client.get.call_args.args == ("https://openrouter.ai/api/v1/key",)
        assert client.get.call_args.kwargs["headers"] == {"Authorization": "Bearer example-key"}
        assert phone_model.plaintext_link_warning() is None
        assert "org/test-model" in phone_model.provider_description()
        client.post.return_value = httpx.Response(200, json={
            "choices": [{"message": {"content": " hello "}}],
        })
        messages = [{"role": "user", "content": "hi"}]
        assert phone_model.chat(messages, max_output_tokens=512) == "hello"
        assert client.post.call_args.args == ("https://openrouter.ai/api/v1/chat/completions",)
        assert client.post.call_args.kwargs["json"] == {
            "model": "org/test-model", "messages": messages, "max_tokens": 512,
            "stream": False,
        }
        assert factory.call_args.kwargs["verify"] is True
        assert factory.call_args.kwargs["trust_env"] is False
        client.post.return_value = httpx.Response(429, text="example-key private prompt")
        with pytest.raises(phone_model.PhoneModelError, match=r"^OpenRouter returned HTTP 429: rate limit reached\.$"):
            phone_model.chat(messages)


def test_openrouter_generation_options_and_caller_cap(monkeypatch):
    monkeypatch.setenv("OPENROUTER_TEMPERATURE", "0.8")
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", "350")
    monkeypatch.setenv("OPENROUTER_REASONING_ENABLED", "false")
    payload = {"max_tokens": 512}
    phone_model._apply_openrouter_options(payload)
    assert payload == {"temperature": 0.8, "max_tokens": 350, "reasoning": {"enabled": False}}
    payload = {"max_tokens": 64}
    phone_model._apply_openrouter_options(payload)
    assert payload["max_tokens"] == 64


@pytest.mark.parametrize(("status", "code", "reason", "delay"), [
    (400, 400, "invalid request", None),
    (401, 401, "authentication failed", None),
    (429, 429, "upstream provider capacity limit reached", 120),
    (200, 429, "upstream provider capacity limit reached", 120),
])
def test_openrouter_safe_provider_errors(monkeypatch, status, code, reason, delay):
    monkeypatch.setenv("PHONE_MODEL_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-test-key")
    monkeypatch.setenv("OPENROUTER_MODEL_NAME", "org/test-model:free")
    with patch.object(phone_model.httpx, "Client") as factory:
        client = factory.return_value.__enter__.return_value
        client.post.return_value = httpx.Response(status, headers={"Retry-After": "120"}, json={
            "error": {
                "code": code, "message": "secret-test-key private-test-message",
                "metadata": {"limit_source": "upstream_provider_shared_pool", "raw": "private-test-message"},
            },
        })
        with pytest.raises(phone_model.OpenRouterError, match=reason) as error:
            phone_model.chat([{"role": "user", "content": "private-test-message"}])
        assert error.value.status == code
        assert error.value.retry_after == delay
        assert "private-test-message" not in str(error.value)
        assert "secret-test-key" not in str(error.value)
        client.post.assert_called_once()


def test_openrouter_timeout_does_not_retry(monkeypatch):
    monkeypatch.setenv("PHONE_MODEL_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL_NAME", "org/test-model:free")
    with patch.object(phone_model.httpx, "Client") as factory:
        client = factory.return_value.__enter__.return_value
        client.post.side_effect = httpx.ReadTimeout("private detail")
        with pytest.raises(phone_model.PhoneModelError, match="OpenRouter request timed out") as error:
            phone_model.chat([{"role": "user", "content": "hello"}])
        assert "private detail" not in str(error.value)
        client.post.assert_called_once()


def test_openrouter_retry_date_and_quota_reset(monkeypatch):
    monkeypatch.setattr(phone_model, "time", lambda: 60)
    response = httpx.Response(429, headers={
        "Retry-After": "Thu, 01 Jan 1970 00:03:00 GMT",
        "X-RateLimit-Reset": "240000",
    })
    error = phone_model._openrouter_error(response, None)
    assert error.retry_after == 180
    nested = phone_model._openrouter_error(httpx.Response(429), {
        "metadata": {"headers": {"X-RateLimit-Reset": "240000"}},
    })
    assert nested.retry_after == 180
    daily = phone_model._openrouter_error(httpx.Response(429), {
        "metadata": {"limit_source": "openrouter_free_tier_daily"},
    })
    assert daily.retry_after == 86340
