"""Three clients using the same node, with no external model or platform calls."""

import json

import pytest
from fastapi.testclient import TestClient

from Ai import e2ee, gateway, miso_discord, miso_roblox, phone_model, server, transport
from Ai.tests.test_miso_memory import (
    GUEST_DM,
    GUILD,
    OWNER_DM,
    PERSONA,
    STYLE,
    chat,
    platform,
    register,
    request,
    start_chat,
)

NODE_TOKEN = "test-only-discord-gateway-token-0123456789"
SESSION = "00000000-0000-4000-8000-000000000001"
REQUEST = "00000000-0000-4000-8000-000000000002"


def test_three_clients_use_one_node_and_separate_audiences(platform, tmp_path, monkeypatch):
    local, memory, _, prompts, _ = platform
    monkeypatch.setenv("SUPER_BOT_DISCORD_NODE_TOKEN", NODE_TOKEN)
    monkeypatch.setenv("SUPER_BOT_DISCORD_DATA_DIR", str(tmp_path / "node-discord"))
    monkeypatch.setenv("DISCORD_OWNER_ID", OWNER_DM.user_id)
    monkeypatch.setenv("DISCORD_CHANNEL_IDS", GUILD.channel_id)
    monkeypatch.setenv("DISCORD_USER_COOLDOWN", "1")
    monkeypatch.setenv("SUPER_BOT_ROBLOX_BRIDGE", "1")

    owner = register(local, "owner")
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"profiles": {"Miso": {
        "persona": PERSONA, "player_name": "Owner", "style": STYLE,
    }}}))
    profile_selection = {"profile_name": "Miso", "profiles_file": str(profiles)}
    assert request(local, "/accounts/miso/publish", profile_selection, owner)["ok"]
    link = request(local, "/accounts/miso/link", {**profile_selection, "discord_id": OWNER_DM.user_id}, owner)

    identity = e2ee.ServerIdentity.generate()
    node = TestClient(gateway.build_gateway(server.app, identity, access_error=None, stream_events=None))

    def secure_call(_url, method, path, body, token, *, timeout):
        data, exchange = e2ee.seal_request(identity.public_key, {
            "method": method, "path": path, "body": body, "authorization": "Bearer " + token,
        })
        response = node.post(gateway.E2EE_PATH, content=data)
        assert response.status_code == 200
        result = exchange.open_response(response.content)
        return result["status"], result["body"]

    monkeypatch.setattr(transport, "secure_request", secure_call)
    discord = miso_discord.NodeDiscord("https://127.0.0.1:8000", NODE_TOKEN, frozenset({GUILD.channel_id}))
    monkeypatch.setenv("PHONE_MODEL_PROVIDER", "phone")
    monkeypatch.setenv("PHONE_MODEL_NAME", "integration-fixture")
    assert "integration-fixture" in discord.request("status", {})["model"]
    assert not prompts
    assert "profile is linked" in discord.reply(OWNER_DM, "100", "/link " + link["code"])
    session = start_chat(local, owner)
    chat(local, session, "TUI private Silver Orchard")
    assert discord.reply(OWNER_DM, "101", "Discord private Copper Harbor") == "Mock reply"
    assert "Silver Orchard" in prompts[-1] and PERSONA in prompts[-1]
    chat(local, session, "What is Copper Harbor?")
    assert "Discord private Copper Harbor" in prompts[-1]

    assert discord.reply(GUEST_DM, "102", "Silver Orchard?") == "Mock reply"
    assert "TUI private" not in prompts[-1] and PERSONA not in prompts[-1]
    assert discord.reply(GUILD, "103", "Copper Harbor?") == "Mock reply"
    assert "Discord private" not in prompts[-1] and PERSONA not in prompts[-1]

    roblox = miso_roblox.RobloxMemory(memory.db)
    roblox.publish_note(owner, "Shareable Blue Meadow")
    _, device_token = memory.issue_device(owner, "fixture", "read-write")
    grant_id, grant_token = roblox.issue(owner, "90", "91")
    headers = {"Authorization": f"Bearer {grant_token}"}
    payload = {
        "universe_id": "90", "player_id": "91", "session_id": SESSION,
        "request_id": REQUEST, "text": "Tell me about Silver Orchard and Copper Harbor",
    }
    assert node.post("/miso/roblox/status", headers=headers, json={"universe_id": "90", "player_id": "91"}).status_code == 200
    assert node.post("/miso/roblox/chat", headers=headers, json={**payload, "player_id": "92"}).status_code == 403
    assert node.post("/miso/roblox/chat", headers=headers, json={**payload, "universe_id": "92"}).status_code == 403
    assert node.post("/miso/roblox/chat", headers={"Authorization": f"Bearer {device_token}"}, json=payload).status_code == 403
    assert secure_call("", "POST", "/miso/search", {"query": "orchard"}, grant_token, timeout=1)[0] == 403
    assert secure_call("", "POST", "/miso/discord/status", {}, grant_token, timeout=1)[0] == 403
    assert node.post("/miso/discord/chat", headers=headers, json={}).status_code == 404

    response = node.post("/miso/roblox/chat", headers=headers, json=payload)
    assert response.status_code == 200 and response.json() == {"reply": "Mock reply"}
    assert PERSONA in prompts[-1] and "Shareable Blue Meadow" in prompts[-1]
    assert "TUI private Silver Orchard" not in prompts[-1]
    assert "Discord private Copper Harbor" not in prompts[-1]
    calls = len(prompts)
    assert node.post("/miso/roblox/chat", headers=headers, json=payload).json() == response.json()
    assert len(prompts) == calls
    assert node.post("/miso/roblox/chat", headers=headers, json={**payload, "text": "different"}).status_code == 409
    assert node.post("/miso/roblox/chat", headers=headers, json={**payload, "request_id": SESSION}).status_code == 429

    # A fresh SDK instance has a new session. Roblox history is never added to private Miso recall.
    monkeypatch.setattr(miso_roblox, "COOLDOWN_SECONDS", 0)
    response = node.post("/miso/roblox/chat", headers=headers, json={
        **payload, "request_id": SESSION, "session_id": REQUEST, "text": "Hello again",
    })
    assert response.status_code == 200
    assert payload["text"] not in prompts[-1]
    assert all(row["source"] != "roblox" for row in memory.device_search(device_token, payload["text"]))
    chat(local, session, "What is Blue Meadow?")
    assert "Shareable Blue Meadow" in prompts[-1]

    local.close("https://server")
    assert discord.reply(OWNER_DM, "104", "Do you remember Blue Meadow?") == "Mock reply"
    assert "Shareable Blue Meadow" in prompts[-1]
    assert node.post("/miso/roblox/chat", headers=headers, json=payload).status_code == 200

    roblox.revoke(owner, grant_id)
    assert node.post("/miso/roblox/chat", headers=headers, json=payload).status_code == 403
    _, replacement = roblox.issue(owner, "90", "91")
    monkeypatch.setattr(miso_roblox, "DAILY_LIMIT", 2)
    assert node.post("/miso/roblox/chat", headers={"Authorization": f"Bearer {replacement}"}, json=payload).status_code == 429
    monkeypatch.delenv("SUPER_BOT_ROBLOX_BRIDGE")
    assert node.post("/miso/roblox/status", headers=headers, json={"universe_id": "90", "player_id": "91"}).status_code == 404

    def rate_limited(*args, **kwargs):
        raise phone_model.GroqError(429, 30, "fixture rate limit")

    with monkeypatch.context() as failing_provider:
        failing_provider.setattr(phone_model, "chat", rate_limited)
        with pytest.raises(miso_discord.NodeRateLimitError) as error:
            discord.reply(OWNER_DM, "105", "Try the unavailable provider")
        assert error.value.status == 429

    miso_discord.backend.cache_clear()
    server._memory_api_database.cache_clear()
    assert discord.reply(OWNER_DM, "105", "Try the unavailable provider") is None
    assert discord.reply(OWNER_DM, "106", "What was Copper Harbor?") == "Mock reply"
    assert "Discord private Copper Harbor" in prompts[-1] and PERSONA in prompts[-1]


def test_public_bridge_bounds_json_and_hides_validation_inputs(platform, monkeypatch):
    monkeypatch.setenv("SUPER_BOT_ROBLOX_BRIDGE", "1")
    node = TestClient(gateway.build_gateway(
        server.app, e2ee.ServerIdentity.generate(), access_error=None, stream_events=None,
    ))
    route = "/miso/roblox/chat"
    assert node.post(route, content="x").status_code == 415
    assert node.post(route, content="x" * 16385, headers={"Content-Type": "application/json"}).status_code == 413
    assert node.post(route, json={"unexpected": "private text"}).json() == {"detail": "Invalid Roblox request"}
