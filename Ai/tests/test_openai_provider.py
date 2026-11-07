"""OpenAI cloud routing, validation, and failure handling without API calls."""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from Ai import phone_model


@pytest.fixture(autouse=True)
def openai_environment(monkeypatch):
    monkeypatch.setenv('PHONE_MODEL_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'secret-test-key')
    monkeypatch.setenv('OPENAI_MODEL_NAME', 'test-cloud-model')
    monkeypatch.setenv('PHONE_MODEL_TIMEOUT_SECONDS', '120')
    # Local provider configuration must not redirect cloud traffic or break TLS.
    monkeypatch.setenv('PHONE_MODEL_BASE_URL', 'http://local-model:8080/v1')
    monkeypatch.setenv('PHONE_MODEL_CA_CERT', '/missing/local-certificate.pem')


@pytest.fixture
def client():
    with patch('Ai.phone_model.httpx.Client') as factory:
        mock = MagicMock()
        factory.return_value.__enter__.return_value = mock
        mock.post.return_value = httpx.Response(200, json={
            'choices': [{'message': {'content': '  Hello  '}}],
        })
        mock.get.return_value = httpx.Response(200, json={'id': 'test-cloud-model'})
        yield mock
        assert factory.call_args.kwargs['verify'] is True
        assert factory.call_args.kwargs['trust_env'] is False


def test_openai_chat_preserves_history_schema_and_model_override(client):
    messages = [{'role': 'system', 'content': 'Return JSON.'},
                {'role': 'user', 'content': 'Hello'}]
    schema = {'type': 'object', 'properties': {'reply': {'type': 'string'}}}
    assert phone_model.chat(messages, schema, model='override-model') == 'Hello'
    request = client.post.call_args
    assert request.args[0] == 'https://api.openai.com/v1/chat/completions'
    assert request.kwargs['headers'] == {'Authorization': 'Bearer secret-test-key'}
    payload = request.kwargs['json']
    assert payload['model'] == 'override-model'
    assert payload['messages'] == messages
    assert payload['response_format']['json_schema']['schema'] == schema
    assert payload['max_completion_tokens'] == 4096
    assert 'max_tokens' not in payload
    assert 'chat_template_kwargs' not in payload


def test_openai_probe_and_description(client):
    phone_model.probe()
    assert client.get.call_args.args[0] == 'https://api.openai.com/v1/models/test-cloud-model'
    assert client.get.call_args.kwargs['headers']['Authorization'] == 'Bearer secret-test-key'
    assert 'OpenAI cloud model (test-cloud-model)' in phone_model.provider_description()
    assert 'secret-test-key' not in phone_model.provider_description()
    assert phone_model.plaintext_link_warning() is None
    phone_model.chat([{'role': 'user', 'content': 'Hi'}])
    assert client.post.call_args.kwargs['json']['model'] == 'test-cloud-model'
    assert 'response_format' not in client.post.call_args.kwargs['json']


@pytest.mark.parametrize('variable,value', [
    ('OPENAI_API_KEY', ''), ('OPENAI_MODEL_NAME', ' '),
    ('PHONE_MODEL_TIMEOUT_SECONDS', 'bad'), ('PHONE_MODEL_TIMEOUT_SECONDS', 'nan'),
    ('PHONE_MODEL_TIMEOUT_SECONDS', '1'),
])
def test_invalid_configuration_fails_before_network(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with patch('Ai.phone_model.httpx.Client') as factory:
        for operation in (phone_model.probe, phone_model.provider_description,
                          lambda: phone_model.chat([])):
            with pytest.raises(phone_model.PhoneModelError, match=variable):
                operation()
        factory.assert_not_called()


@pytest.mark.parametrize('status,match', [(401, 'OPENAI_API_KEY'), (403, 'OPENAI_API_KEY'),
                                        (429, 'HTTP 429'), (500, 'HTTP 500'), (404, 'HTTP 404')])
def test_cloud_errors_do_not_echo_response_body(client, status, match):
    response = httpx.Response(status, text='secret-test-key private upstream details')
    client.post.return_value = client.get.return_value = response
    for operation in (phone_model.probe, lambda: phone_model.chat([])):
        with pytest.raises(phone_model.PhoneModelError, match=match) as error:
            operation()
        assert 'secret-test-key' not in str(error.value)
        assert 'private upstream' not in str(error.value)


@pytest.mark.parametrize('payload', [[], {}, {'choices': []},
    {'choices': [{'message': {'content': None, 'refusal': 'Declined'}}]},
    {'choices': [{'message': {'content': ' '}, 'finish_reason': 'length'}]},
])
def test_malformed_or_empty_replies(client, payload):
    client.post.return_value = httpx.Response(200, json=payload)
    with pytest.raises(phone_model.PhoneModelError, match='OpenAI'):
        phone_model.chat([])


def test_invalid_json_and_timeout(client):
    client.post.return_value = httpx.Response(200, text='not JSON')
    with pytest.raises(phone_model.PhoneModelError, match='OpenAI returned invalid JSON'):
        phone_model.chat([])
    client.post.side_effect = httpx.ReadTimeout('timeout')
    with pytest.raises(phone_model.PhoneModelError, match='OpenAI request timed out'):
        phone_model.chat([])
    client.get.side_effect = httpx.ConnectError('connection failed')
    with pytest.raises(phone_model.PhoneModelError, match='readiness check'):
        phone_model.probe()
