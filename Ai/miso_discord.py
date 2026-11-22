"""Trusted Discord gateway adapter for the node's sealed Miso routes."""

import hmac
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from Ai import phone_model, tls, transport
from Ai.discord_backend import DiscordBackend, Identity

DiscordID = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$", max_length=20)]


class DiscordMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: DiscordID
    channel_id: DiscordID
    guild_id: DiscordID | None = None
    message_id: DiscordID
    text: str = Field(min_length=1, max_length=1800)


def authorize(token):
    expected = os.getenv("SUPER_BOT_DISCORD_NODE_TOKEN", "").strip()
    if len(expected) < 32 or not expected.isascii():
        raise HTTPException(404, "Discord node integration disabled")
    if not token or not token.isascii() or not hmac.compare_digest(token, expected):
        raise HTTPException(403, "Discord gateway authorization required")


@lru_cache(maxsize=1)
def backend(db):
    from Ai.discord_app import env_integer, snowflake
    from Ai.miso_memory import MemoryStore

    owner = os.getenv("DISCORD_OWNER_ID", "").strip()
    channels = frozenset(os.getenv("DISCORD_CHANNEL_IDS", "").replace(",", " ").split())
    if not snowflake(owner) or not all(map(snowflake, channels)):
        raise HTTPException(503, "Discord node identity configuration invalid")
    directory = Path(os.getenv("SUPER_BOT_DISCORD_DATA_DIR") or tls.app_data_dir() / "discord")
    return DiscordBackend(
        directory.expanduser(), owner, channels,
        env_integer("DISCORD_DAILY_REQUESTS", 60, 1, 10000),
        env_integer("DISCORD_USER_COOLDOWN", 10, 1, 3600),
        memory=MemoryStore(db),
    )


class NodeRateLimitError(phone_model.ProviderError):
    provider_name = "Discord node"


class NodeDiscord:
    def __init__(self, url, token, channels):
        transport.parse_server_url(url)
        self.url = url
        self.token = token
        self.channels = channels

    def request(self, path, payload, *, timeout=125):
        try:
            status, result = transport.secure_request(
                self.url, "POST", f"/miso/discord/{path}", payload, self.token, timeout=timeout,
            )
        except transport.TransportError:
            raise phone_model.PhoneModelError("Discord node unreachable or fingerprints untrusted") from None
        if status == 429:
            raise NodeRateLimitError(status, None, "reply service rate limited")
        if status != 200 or not isinstance(result, dict):
            raise phone_model.PhoneModelError(f"Discord node returned HTTP {status}")
        return result

    def reply(self, identity, message_id, text):
        result = self.request("chat", {
            "user_id": identity.user_id, "channel_id": identity.channel_id,
            "guild_id": identity.guild_id, "message_id": message_id, "text": text[:1800],
        })
        reply = result.get("reply")
        return reply if isinstance(reply, str) else None
