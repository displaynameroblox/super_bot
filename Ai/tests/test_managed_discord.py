from pathlib import Path
from unittest.mock import MagicMock, patch

from Ai.manager_worker import managed_discord


def test_disabled_discord_does_not_create_a_process(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPER_BOT_DISCORD_ENV", raising=False)
    with patch("Ai.manager_worker.subprocess.Popen") as popen:
        with managed_discord(tmp_path, tmp_path, "test") as state:
            assert state() == "disabled"
    popen.assert_not_called()


def test_discord_ready_failure_and_parent_shutdown(tmp_path, monkeypatch):
    config = tmp_path / ".discord.env"
    monkeypatch.setenv("SUPER_BOT_DISCORD_ENV", str(config))
    bot = MagicMock()
    bot.poll.return_value = None
    with patch("Ai.manager_worker.subprocess.Popen", return_value=bot) as popen:
        with managed_discord(tmp_path, tmp_path, "test") as state:
            assert state() == "connecting"
            ready = Path(popen.call_args.kwargs["env"]["SUPER_BOT_DISCORD_READY"])
            ready.touch()
            assert state() == "ready"
            bot.poll.return_value = 1
            assert state() == "failed"
    bot.stdin.close.assert_called_once()
    bot.wait.assert_called_once_with(timeout=3)
    assert not ready.exists()
