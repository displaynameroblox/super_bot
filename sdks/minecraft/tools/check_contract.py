"""Validate Java-generated capability schemas and lifecycle against the real SBP host."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from Ai.universal import contracts as c
from Ai.universal.host import Host, ProtocolError
from Ai.universal.security import Principal, digest
from Ai.universal.store import Store
from Ai.universal.runtime import Runtime

caps = [c.Capability.model_validate(v) for v in json.loads(
    (Path(__file__).resolve().parents[1] / 'common/build/fixtures/capabilities.json').read_text())]
with TemporaryDirectory() as tmp:
    store = Store(Path(tmp) / 'host.db')
    host = Host(store, [Principal('sdk', digest('test-bootstrap'), 'adapter',
        frozenset({'world'}), frozenset(v.id for v in caps), frozenset(v.id for v in caps),
        models=frozenset({'chatter'}))])
    token = host.start_session('test-bootstrap', c.SessionRequest())['access_token']
    def call(op, **kw): return host.invoke(token, op, **kw)
    call('register_environment', req=c.EnvironmentRequest(id='world', name='Minecraft', kind='minecraft', capabilities=caps))
    call('register_agent', req=c.AgentRequest(id='bot', name='Super-Bot', environments=['world'], capabilities=[v.id for v in caps]))
    runtime = Runtime(host)
    task = asyncio.run(runtime.run(token, 'bot', c.TaskRequest(goal='Observe without a model', actions=[
        c.ActionRequest(id='observe-1', environment_id='world', capability='minecraft.observe', version='1')
    ])))
    assert task['state'] == 'waiting'
    for state in ('acknowledged', 'running', 'completed'):
        call('update_action', environment_id='world', agent_id='bot', action_id='observe-1',
             req=c.ActionUpdate(state=state, progress=1.0 if state == 'completed' else 0.0,
                                result={'x': 1, 'y': 64, 'z': 1} if state == 'completed' else None))
    assert call('tasks', agent_id='bot')[0]['state'] == 'completed'
    assert call('poll_actions', environment_id='world') == []
    try:
        asyncio.run(runtime.run(token, 'bot', c.TaskRequest(goal='Hello', model='chatter')))
        raise AssertionError('Missing Chatter must not silently fall back')
    except ProtocolError as exc:
        assert exc.body['error']['code'] == 'model_unavailable'
    store.close()
print('Java capabilities, SBP action lifecycle, model-free task and missing-Chatter checks passed')
