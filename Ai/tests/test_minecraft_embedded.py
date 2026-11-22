"""The optional Minecraft gateway shares the normal server without changing chat."""
import hashlib
import json
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from Ai.universal.embedded import attach


def test_opt_in_sbp_on_existing_https_server(tmp_path):
    token = 'high-entropy-test-credential'
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'principals': [{'id':'adapter','role':'adapter',
        'token_hash':hashlib.sha256(token.encode()).hexdigest(),
        'environments':['minecraft-npc'],'capabilities':['minecraft.observe'],
        'permissions':['minecraft.observe'],'models':['existing']}],
        'existing_model_provider':True}))
    os.chmod(config, 0o600)
    gateway = FastAPI()
    @gateway.get('/health')
    def health(): return {'status':'chat-ok'}
    store = attach(gateway, config, tmp_path / 'sbp')
    try:
        with TestClient(gateway, base_url='https://testserver') as client:
            assert client.get('/health').json() == {'status':'chat-ok'}
            node = client.get('/.well-known/superbot').json()
            assert node['node_id'].startswith('nod_')
            assert node['status'] == 'ready'
            assert node['sbp_path'] == '/sbp/v0.1'
            assert client.get('/sbp/v0.1/protocol').json()['versions'] == ['0.1']
            assert client.get('/sbp/v0.1/models').status_code == 401
            assert client.post('/sbp/v0.1/sessions', json={'versions':['0.1']}).status_code == 401
            grant = client.post('/sbp/v0.1/sessions', json={'versions':['0.1']},
                                headers={'Authorization':'Bearer '+token})
            assert grant.status_code == 200
            assert grant.json()['principal_id'] == 'adapter'
            headers = {'Authorization': 'Bearer ' + grant.json()['access_token']}
            assert client.get('/sbp/v0.1/models', headers=headers).json() == {'routes': ['existing']}
    finally:
        store.close()
    restarted = FastAPI()
    store = attach(restarted, config, tmp_path / 'sbp')
    try:
        with TestClient(restarted, base_url='https://testserver') as client:
            assert client.get('/.well-known/superbot').json() == node
    finally:
        store.close()


def test_model_and_movement_use_normal_server_provider(tmp_path, monkeypatch):
    from Ai import phone_model
    token = 'another-high-entropy-test-credential'
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'principals': [{'id':'adapter','role':'adapter',
        'token_hash':hashlib.sha256(token.encode()).hexdigest(),
        'environments':['world'],'capabilities':['minecraft.move_to'],
        'permissions':['minecraft.move_to'],'models':['existing']}],
        'existing_model_provider':True}))
    os.chmod(config, 0o600)
    monkeypatch.setattr(phone_model, 'chat', lambda messages: '{"x":3,"y":64,"z":0}')
    gateway = FastAPI()
    store = attach(gateway, config, tmp_path / 'sbp')
    try:
        with TestClient(gateway, base_url='https://testserver') as client:
            prefix='/sbp/v0.1'
            grant=client.post(prefix+'/sessions', json={'versions':['0.1']}, headers={'Authorization':'Bearer '+token}).json()
            headers={'Authorization':'Bearer '+grant['access_token']}
            cap={'id':'minecraft.move_to','version':'1','input_schema':{'type':'object'},
                 'output_schema':{'type':'object'},'permissions':['minecraft.move_to']}
            assert client.post(prefix+'/environments', headers=headers, json={'id':'world','name':'Minecraft','kind':'minecraft','capabilities':[cap]}).status_code==200
            assert client.post(prefix+'/agents', headers=headers, json={'id':'bot','name':'Super-Bot','environments':['world'],
                'capabilities':['minecraft.move_to']}).status_code==200
            result=client.post(prefix+'/agents/bot/tasks', headers=headers, json={'goal':'Choose a destination', 'model':'existing'})
            assert result.status_code==200
            assert json.loads(result.json()['response'])=={'x':3,'y':64,'z':0}
            action=client.post(prefix+'/agents/bot/actions',headers=headers,json={'id':'one','environment_id':'world',
                'capability':'minecraft.move_to','version':'1','input':json.loads(result.json()['response'])})
            assert action.status_code==200 and action.json()['state']=='queued'
            assert client.get(prefix+'/environments/world/actions',headers=headers).json()[0]['id']=='one'
    finally:
        store.close()


def test_normal_server_start_mounts_sbp_only_when_enabled(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from Ai import server
    token='startup-test-credential'
    config=tmp_path / 'config.json'
    config.write_text(json.dumps({'principals':[{'id':'sdk','role':'adapter',
        'token_hash':hashlib.sha256(token.encode()).hexdigest(),
        'environments':['world'],'capabilities':['minecraft.observe']}]}))
    os.chmod(config, 0o600)
    monkeypatch.setenv('SUPER_BOT_SERVER_DIR',str(tmp_path / 'normal-server'))
    monkeypatch.setenv('SUPER_BOT_SBP_CONFIG',str(config))
    monkeypatch.setenv('HOST','127.0.0.1')
    monkeypatch.setenv('PORT','8000')
    monkeypatch.setenv('DISCOVERY_PORT','8766')
    monkeypatch.delenv('SUPER_BOT_SBP_DATA_DIR',raising=False)
    monkeypatch.setattr(server, 'ensure_port_available', lambda *args: None)
    monkeypatch.setattr(server, 'init_persistence', lambda: SimpleNamespace(path=tmp_path / 'normal.db'))
    monkeypatch.setattr(server, 'socket', SimpleNamespace(AF_INET=2,SOCK_DGRAM=2,SOL_SOCKET=1,SO_REUSEADDR=2,socket=Mock()))
    monkeypatch.setattr(server, 'threading', SimpleNamespace(Thread=Mock()))
    observed=[]
    def run(app, **options):
        with TestClient(app, base_url='https://testserver') as client:
            assert client.get('/health').status_code == 200
            assert client.get('/e2ee/identity').status_code == 200
            assert client.post('/start_ses', json={}).status_code == 404
            assert client.get('/sbp/v0.1/protocol').json()['versions'] == ['0.1']
            grant=client.post('/sbp/v0.1/sessions',json={'versions':['0.1']},headers={'Authorization':'Bearer '+token})
            assert grant.status_code == 200
            observed.append(True)
    monkeypatch.setattr(server.uvicorn,'run',run)
    server.start_server()
    assert observed == [True]


def test_opt_in_manager_shares_selected_local_model_without_leaking_environment(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, Mock, patch
    from Ai.managed_model import local_model
    model=tmp_path/'test.gguf'; model.touch()
    (tmp_path/'model.json').write_text(json.dumps({'path':str(model)}))
    monkeypatch.setenv('SUPER_BOT_SBP_CONFIG',str(tmp_path/'config.json'))
    monkeypatch.setenv('PHONE_MODEL_PROVIDER','phone')
    monkeypatch.delenv('PHONE_MODEL_BASE_URL',raising=False)
    monkeypatch.delenv('TEST_PHONE_API_KEY',raising=False)
    child=Mock(); child.poll.return_value=None
    client=MagicMock(); client.__enter__.return_value.get.return_value.json.return_value={'data':[{'id':'superbot-phone'}]}
    with patch('Ai.managed_model.shutil.which',return_value='/bin/llama-server'), \
         patch('Ai.managed_model.socket.socket') as sock, \
         patch('Ai.managed_model.subprocess.Popen',return_value=child), patch('httpx.Client',return_value=client):
        sock.return_value.__enter__.return_value.getsockname.return_value=('127.0.0.1',43210)
        with local_model(tmp_path,'launch'):
            assert os.environ['PHONE_MODEL_BASE_URL']=='http://127.0.0.1:43210/v1'
            assert os.environ['PHONE_MODEL_NAME']=='superbot-phone'
            assert os.environ['TEST_PHONE_API_KEY']
        assert 'PHONE_MODEL_BASE_URL' not in os.environ
        assert 'TEST_PHONE_API_KEY' not in os.environ
        child.terminate.assert_called_once()


def test_existing_provider_limits_observations_for_small_local_model(monkeypatch):
    import asyncio
    from Ai import phone_model
    from Ai.universal.runtime import ExistingProvider
    captured=[]
    monkeypatch.setattr(phone_model,'chat',lambda messages: captured.extend(messages) or 'hello')
    payload={'goal':'Hello','events':[{'data':'x'*500} for _ in range(100)],'knowledge':[{'data':'y'*3000}]}
    assert asyncio.run(ExistingProvider().generate([{'role':'user','content':json.dumps(payload)}]))=='hello'
    actual=json.loads(captured[-1]['content'])
    assert actual['goal']=='Hello'
    assert len(json.dumps(actual))<=1200
    assert len(actual['events'])<=1


def test_manager_gemini_selection_does_not_launch_selected_gguf(tmp_path, monkeypatch):
    from unittest.mock import patch
    from Ai.managed_model import local_model

    (tmp_path / 'model.json').write_text(json.dumps({'path': str(tmp_path / 'qwen.gguf')}))
    (tmp_path / '.env').write_text('PHONE_MODEL_PROVIDER=gemini\n')
    monkeypatch.delenv('PHONE_MODEL_PROVIDER', raising=False)
    with patch('Ai.managed_model.subprocess.Popen') as launch:
        with local_model(tmp_path, 'launch') as runner:
            assert runner is None
        launch.assert_not_called()


def test_movement_request_omits_duplicate_history_to_leave_room_for_json(monkeypatch):
    import asyncio
    from Ai import phone_model
    from Ai.universal.runtime import ExistingProvider
    captured=[]
    monkeypatch.setattr(phone_model,'chat',lambda messages: captured.extend(messages) or '{"x":1,"y":64,"z":2}')
    goal='You control a Minecraft NPC. Return ONLY a single JSON object with coordinates.'
    payload={'goal':goal,'events':[{'data':'long' * 150}],'knowledge':[{'content':'long' * 150}]}
    asyncio.run(ExistingProvider().generate([{'role':'user','content':json.dumps(payload)}]))
    actual=json.loads(captured[-1]['content'])
    assert actual['goal']==goal
    assert actual['events']==[] and actual['knowledge']==[]


def test_private_chat_survives_restart_and_is_scoped(tmp_path, monkeypatch):
    from uuid import uuid4
    from Ai import phone_model

    credential = 'private-chat-test-token'
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({
        'existing_model_provider': True,
        'principals': [{
            'id': 'sdk', 'role': 'adapter',
            'token_hash': hashlib.sha256(credential.encode()).hexdigest(),
            'environments': ['world'], 'models': ['existing'],
        }],
    }))
    config.chmod(0o600)
    prompts = []

    def chat(messages):
        prompts.append(messages)
        return 'Short reply'

    monkeypatch.setattr(phone_model, 'chat', chat)
    prefix = '/sbp/v0.1'
    endpoint = prefix + '/agents/bot/private-chat'
    scope = {'environment_id': 'world', 'world_id': str(uuid4()), 'player_id': str(uuid4())}
    first = {**scope, 'request_id': str(uuid4()), 'model': 'existing', 'text': 'Call me Alex'}

    from contextlib import contextmanager

    @contextmanager
    def connection():
        app = FastAPI()
        store = attach(app, config, tmp_path / 'sbp')
        try:
            with TestClient(app, base_url='https://testserver') as client:
                grant = client.post(prefix + '/sessions', json={'versions': ['0.1']},
                                    headers={'Authorization': 'Bearer ' + credential})
                headers = {'Authorization': 'Bearer ' + grant.json()['access_token']}
                yield client, headers
        finally:
            store.close()

    with connection() as (client, headers):
        assert client.post(prefix + '/environments', headers=headers, json={
            'id': 'world', 'name': 'Minecraft', 'kind': 'minecraft', 'capabilities': [],
        }).status_code == 200
        assert client.post(prefix + '/agents', headers=headers, json={
            'id': 'bot', 'name': 'Super-Bot', 'environments': ['world'], 'capabilities': [],
        }).status_code == 200
        assert client.post(endpoint, json=first).status_code == 401
        response = client.post(endpoint, headers=headers, json=first)
        assert response.status_code == 200
        original = response.json()

    with connection() as (client, headers):
        def send(**fields):
            return client.post(endpoint, headers=headers, json={
                **first, 'request_id': str(uuid4()), 'text': 'What is my name?', **fields,
            })

        assert client.post(endpoint, headers=headers, json=first).json() == original
        assert len(prompts) == 1
        assert send(request_id=first['request_id']).status_code == 409
        assert send().json()['conversation_id'] == original['conversation_id']
        assert any(message['content'] == 'Call me Alex' for message in prompts[-1])
        for field in ('world_id', 'player_id'):
            response = send(**{field: str(uuid4())})
            assert response.status_code == 200
            assert response.json()['conversation_id'] != original['conversation_id']
            assert len(prompts[-1]) == 2
        assert send(environment_id='other').status_code == 403
        assert client.post(endpoint + '/reset', headers=headers, json=scope).status_code == 200
        assert send().json()['conversation_id'] != original['conversation_id']
        assert len(prompts[-1]) == 2
        events = client.get(prefix + '/agents/bot/events', headers=headers).text
        assert 'Call me Alex' not in events
