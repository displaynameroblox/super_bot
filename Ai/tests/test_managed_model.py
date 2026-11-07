import os
import shlex
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from Ai.admin import action
from Ai.managed_model import local_model
from Ai.manager_runtime import atomic_json


def test_selected_gguf_provisions_provider_and_stops_runner(tmp_path, monkeypatch):
    monkeypatch.setenv('PHONE_MODEL_PROVIDER', 'phone')
    model = tmp_path / 'test model.gguf'
    model.touch()
    action('model select ' + shlex.quote(str(model)), SimpleNamespace(root=tmp_path), None)
    child = Mock()
    child.poll.return_value = None
    client = MagicMock()
    client.__enter__.return_value.get.return_value.json.return_value = {
        'data': [{'id': 'superbot-phone'}],
    }
    with patch.dict(os.environ), patch('Ai.managed_model.shutil.which', return_value='/bin/llama-server'), \
            patch('Ai.managed_model.socket.socket'), patch('Ai.managed_model.subprocess.Popen', return_value=child) as start, \
            patch('httpx.Client', return_value=client):
        with local_model(tmp_path, 'launch') as running:
            assert running is child
            assert os.environ['PHONE_MODEL_PROVIDER'] == 'phone'
            assert os.environ['PHONE_MODEL_NAME'] == 'superbot-phone'
            assert str(model) in start.call_args.args[0]
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=3)


def test_cancel_during_model_load_stops_runner(tmp_path, monkeypatch):
    monkeypatch.setenv('PHONE_MODEL_PROVIDER', 'phone')
    model = tmp_path / 'test.gguf'
    model.touch()
    atomic_json(tmp_path / 'model.json', {'path': str(model)})
    atomic_json(tmp_path / 'stop.json', {'startup_id': 'launch'})
    child = Mock()
    child.poll.return_value = None
    with patch('Ai.managed_model.shutil.which', return_value='/bin/llama-server'), \
            patch('Ai.managed_model.socket.socket'), patch('Ai.managed_model.subprocess.Popen', return_value=child), \
            patch('httpx.Client'), pytest.raises(RuntimeError, match='startup was stopped'):
        with local_model(tmp_path, 'launch'):
            pytest.fail('Cancelled startup must not run the server')
    child.terminate.assert_called_once()


def test_missing_runner_requires_confirmation_before_start(tmp_path, monkeypatch):
    from Ai.admin import SetupRequired

    monkeypatch.setenv('PHONE_MODEL_PROVIDER', 'phone')
    model = tmp_path / 'test.gguf'
    model.touch()
    atomic_json(tmp_path / 'model.json', {'path': str(model)})
    with patch('Ai.admin.sys.platform', 'darwin'), \
            patch('Ai.managed_model.runner_executable', return_value=None), \
            patch('Ai.admin.shutil.which', return_value='/opt/homebrew/bin/brew'), \
            patch('Ai.admin.subprocess.run') as run, \
            pytest.raises(SetupRequired) as prompt:
        action('restart', SimpleNamespace(root=tmp_path), None)
    assert prompt.value.command == ['/opt/homebrew/bin/brew', 'install', 'llama.cpp']
    run.assert_not_called()


def test_explicit_cloud_provider_skips_saved_local_model(tmp_path, monkeypatch):
    from Ai.managed_model import check_model_ready

    atomic_json(tmp_path / 'model.json', {'path': '/missing/model.gguf'})
    monkeypatch.setenv('PHONE_MODEL_PROVIDER', 'openai')
    check_model_ready(tmp_path)
    with patch('Ai.managed_model.subprocess.Popen') as start:
        with local_model(tmp_path, 'launch') as model:
            assert model is None
        start.assert_not_called()
