from unittest.mock import patch
from Ai.manager_worker import write_telemetry


def test_telemetry_disk_full_does_not_kill_control_loop(tmp_path):
    with patch('Ai.manager_worker.atomic_json', side_effect=OSError(28, 'No space left on device')):
        assert write_telemetry(tmp_path, {'sessions': 0}) is False
    assert write_telemetry(tmp_path, {'sessions': 0}) is True
