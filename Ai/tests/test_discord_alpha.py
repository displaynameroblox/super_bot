import asyncio
import json
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from Ai import discord_app, phone_model
from Ai.discord_backend import DiscordBackend, Identity


def test_owner_import_is_scoped_and_sources_are_preserved(tmp_path, monkeypatch):
    profile = tmp_path / "chat_profiles.json"
    profile.write_text(json.dumps({"profiles": {"Miso": {
        "player_name": "Private Player", "persona": "Private Persona",
        "style": "Private Style", "chat_skills": {"roleplay": "Private Skill"},
        "character_development_enabled": True, "character_notes": "Private Notes",
        "chat_log": "AI: Private History",
    }}}))
    memory = tmp_path / "knowledge.db"
    with closing(sqlite3.connect(memory)) as db, db:
        db.execute("CREATE TABLE passages (id INTEGER PRIMARY KEY, content TEXT)")
        db.execute("INSERT INTO passages VALUES (1, 'apricot Private Memory')")
    originals = profile.read_bytes(), memory.read_bytes()
    backend = DiscordBackend(tmp_path / "discord", "101", frozenset({"901", "902"}), 60, 1)
    backend.import_owner(profile, "miso", copy_history=True, knowledge_file=memory)
    backend.import_owner(profile, "Miso", copy_history=True, knowledge_file=memory)
    backend.import_owner(profile, "Miso")
    calls = []

    def model(messages, *, max_output_tokens):
        calls.append(json.dumps(messages))
        assert max_output_tokens == 512
        return "test reply"

    monkeypatch.setattr(phone_model, "chat", model)
    with patch("Ai.discord_backend.time.time", side_effect=[10, 12, 14, 16, 18, 20, 22]):
        assert backend.reply(Identity("101", "800"), "1", "apricot owner-DM-only")
        assert backend.reply(Identity("202", "801"), "2", "guest-DM-only")
        assert backend.reply(Identity("101", "901", "900"), "3", "apricot channel-one")
        assert backend.reply(Identity("202", "901", "900"), "4", "apricot follow up")
        assert backend.reply(Identity("101", "902", "900"), "5", "apricot channel-two")
        assert backend.reply(Identity("101", "909", "900"), "6", "disallowed") is None
        assert backend.reply(Identity("202", "801"), "7", "apricot what do you remember?")
        assert backend.reply(Identity("101", "800"), "8", "apricot what do you remember?")
    for value in ("Player", "Persona", "Style", "Skill", "Notes", "History", "Memory"):
        assert f"Private {value}" in calls[0]
        assert f"Private {value}" in calls[-1]
        assert all(f"Private {value}" not in request for request in calls[1:-1])
    assert "owner-DM-only" in calls[-1]
    assert "owner-DM-only" not in calls[-2]
    assert "guest-DM-only" in calls[-2]
    assert "guest-DM-only" not in calls[-1]
    assert "channel-one" in calls[3]
    assert "channel-one" not in calls[4]
    assert all("cuddly calico" in call for call in calls[1:-1])
    assert (profile.read_bytes(), memory.read_bytes()) == originals
    with pytest.raises(PermissionError, match="different owner"):
        DiscordBackend(tmp_path / "discord", "202", frozenset(), 60, 1)


def test_rate_limit_survives_restart_and_failed_calls_count(tmp_path):
    path = tmp_path / "discord"
    def make_backend():
        return DiscordBackend(path, "101", frozenset({"900"}), 2, 10)

    backend = make_backend()
    with patch("Ai.discord_backend.time.time") as clock, patch.object(phone_model, "chat") as model:
        clock.return_value = 100
        model.side_effect = RuntimeError("private provider details")
        with pytest.raises(RuntimeError):
            backend.reply(Identity("101", "800"), "1", "first")
        backend = make_backend()
        clock.return_value = 105
        assert backend.reply(Identity("101", "900", "700"), "2", "cooldown") is None
        clock.return_value = 110
        assert backend.reply(Identity("101", "800"), "1", "duplicate") is None
        model.side_effect = None
        model.return_value = "ok"
        assert backend.reply(Identity("202", "801"), "2", "second") == "ok"
        assert backend.reply(Identity("303", "802"), "3", "over budget") is None
        clock.return_value = 86401
        assert backend.reply(Identity("303", "802"), "3", "next day") == "ok"
        assert model.call_count == 3


@pytest.mark.parametrize("provider", ["openai", "gemini", "openrouter", "groq"])
def test_cloud_startup_skips_the_local_manager(provider, monkeypatch):
    monkeypatch.delenv("DISCORD_PROVIDER_TIMEOUT", raising=False)
    monkeypatch.setenv("PHONE_MODEL_PROVIDER", "phone")
    with patch("Ai.managed_model.local_model") as local_model:
        with discord_app.inference_context(provider, None):
            assert discord_app.os.environ["PHONE_MODEL_PROVIDER"] == provider
        local_model.assert_not_called()


def test_local_startup_selects_managed_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_MODEL_PROVIDER", "openai")
    monkeypatch.delenv("DISCORD_PROVIDER_TIMEOUT", raising=False)
    with patch("Ai.managed_model.selected_model_path", return_value=tmp_path / "model.gguf"), \
         patch("Ai.managed_model.local_model") as runner:
        with discord_app.inference_context("local", tmp_path):
            assert discord_app.os.environ["PHONE_MODEL_PROVIDER"] == "phone"
        assert runner.call_args.args[0] == tmp_path
        assert runner.call_args.kwargs == {"quiet": True, "startup_timeout": 90}


def test_private_environment_permissions_and_precedence(tmp_path, monkeypatch):
    env = tmp_path / ".discord.env"
    env.write_text("DISCORD_BOT_TOKEN=test-file-token\n")
    if discord_app.os.name != "nt":
        env.chmod(0o644)
        with pytest.raises(discord_app.ConfigurationError, match="private"):
            discord_app.load_private_env(env, required=True)
    env.chmod(0o600)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "environment-token")
    discord_app.load_private_env(env, required=True)
    assert discord_app.os.environ["DISCORD_BOT_TOKEN"] == "environment-token"


def test_message_routing():
    class DMChannel:
        id = 600

    class TextChannel:
        id = 700

    discord = SimpleNamespace(DMChannel=DMChannel, TextChannel=TextChannel,
                              MessageType=SimpleNamespace(default=0, reply=1))
    msg = SimpleNamespace(author=SimpleNamespace(id=101, bot=False), webhook_id=None,
                          type=0, channel=TextChannel(), guild=SimpleNamespace(id=800),
                          content="hello")
    channels = frozenset({"700"})
    assert discord_app.route_message(discord, 42, msg, True, channels) is None
    assert discord_app.route_message(discord, 42, msg, False, channels)[1] == "hello"
    msg.content = "<@!42> hello"
    assert discord_app.route_message(discord, 42, msg, True, channels) == (
        Identity("101", "700", "800"), "hello"
    )
    assert discord_app.route_message(discord, 42, msg, True, frozenset()) is None
    msg.channel, msg.guild, msg.content = DMChannel(), None, "hello"
    assert discord_app.route_message(discord, 42, msg, True, channels) == (
        Identity("101", "600"), "hello"
    )
    msg.author.bot = True
    assert discord_app.route_message(discord, 42, msg, True, channels) is None
    msg.author.bot, msg.channel = False, SimpleNamespace(id=601)
    assert discord_app.route_message(discord, 42, msg, True, channels) is None


def test_provider_cooldown_and_failed_history(tmp_path, monkeypatch):
    from Ai import discord_backend, phone_model

    backend = DiscordBackend(tmp_path / "discord", "101", frozenset(), 60, 1)
    now = [100.0]
    monkeypatch.setattr(discord_backend.time, "time", lambda: now[0])
    monkeypatch.setattr(discord_backend.time, "monotonic", lambda: now[0])
    identity = Identity("101", "500")
    with patch.object(phone_model, "chat", side_effect=[
        phone_model.OpenRouterError(429, 120, "rate limit reached"), "Hello again",
    ]) as chat:
        with pytest.raises(phone_model.OpenRouterError):
            backend.reply(identity, "1", "first")
        now[0] = 105
        assert "temporarily unavailable" in backend.reply(identity, "2", "during cooldown")
        assert backend.reply(identity, "2", "during cooldown") is None
        assert chat.call_count == 1
        now[0] = 220
        assert backend.reply(identity, "3", "after cooldown") == "Hello again"
        assert chat.call_count == 2
    with backend._connect() as db:
        assert [tuple(row) for row in db.execute("SELECT prompt, reply FROM history")] == [
            ("after cooldown", "Hello again"),
        ]


def test_discord_private_env_is_used_without_overwriting_process_values(tmp_path, monkeypatch):
    from Ai.discord_app import load_private_env, selected_provider

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("DISCORD_PROVIDER", "gemini")
    env_file = tmp_path / ".discord.env"
    env_file.write_text("OPENROUTER_API_KEY=test-key\nDISCORD_PROVIDER=openrouter\n")
    env_file.chmod(0o600)
    load_private_env(env_file, required=True)
    assert os.environ["OPENROUTER_API_KEY"] == "test-key"
    assert selected_provider(None) == "gemini"
    assert selected_provider("openrouter") == "openrouter"
    monkeypatch.delenv("OPENROUTER_API_KEY")


@pytest.mark.parametrize(("error", "feedback"), [
    (phone_model.OpenRouterError(429, 120, "rate limit reached"), "rate limited"),
    (phone_model.OpenRouterError(400, None, "invalid request"), "rejected the request"),
    (phone_model.OpenRouterError(401, None, "authentication failed"), "rejected the request"),
    (phone_model.PhoneModelError("private error details"), "couldn't reply"),
])
def test_gateway_sends_one_safe_provider_failure(monkeypatch, capsys, error, feedback):
    pytest.importorskip("discord")
    import discord
    from unittest.mock import AsyncMock, Mock

    async def deliver(client):
        channel = SimpleNamespace(send=AsyncMock())
        await client.on_message(SimpleNamespace(id=1, channel=channel))
        channel.send.assert_awaited_once()
        assert feedback in channel.send.call_args.args[0]

    monkeypatch.setattr(discord.Client, "user", SimpleNamespace(id=12))
    monkeypatch.setattr(discord.Client, "run", lambda client, *args, **kwargs: asyncio.run(deliver(client)))
    monkeypatch.setattr(discord_app, "route_message", lambda *args: (Identity("101", "500"), "private prompt"))
    backend = SimpleNamespace(channels=frozenset(), reply=Mock(side_effect=error))
    discord_app.run_gateway("private token", backend, True)
    backend.reply.assert_called_once()
    assert "private" not in capsys.readouterr().err
