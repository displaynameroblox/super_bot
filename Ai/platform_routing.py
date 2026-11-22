"""Read-only chat handoff routing. SDK action grants are separate."""
from dataclasses import dataclass
import re


PLATFORMS = ("tui", "web", "api", "discord_dm", "discord_guild", "minecraft", "roblox")
TOOLS = {"discord": "discord.handoff", "minecraft": "minecraft.handoff"}
_INVITATION = re.compile(
    r"(?:/handoff\s+|(?:miso,?\s+)?(?:come (?:over )?to|hop (?:over )?to|switch to|"
    r"(?:can you |please )?join(?: me in)?)\s+(?:my |the )?)"
    r"(discord|minecraft)(?: (?:dm|world))?[.!?]*", re.IGNORECASE,
)


@dataclass(frozen=True)
class Handoff:
    tool: str
    outcome: str
    reply: str


def destination(text):
    match = _INVITATION.fullmatch(text.strip())
    return match[1].lower() if match else None


def route_handoff(target, source, availability, *, linked=None, character_present=False):
    """Call only from accepted chat turns. No arbitrary destination or payload."""
    tool = TOOLS[target]
    if target == "discord":
        if source.startswith("discord_"):
            return Handoff(tool, "already_here", "We’re already chatting on Discord :3")
        if availability(target) == "ready":
            reply = "Open my Discord DM and send me a message there. We can keep chatting :3"
            if linked is not True:
                reply += " Shared private memory requires a linked account."
            return Handoff(tool, "ready", reply)
        return Handoff(tool, "unverified", "I can’t verify that my Discord bot is ready right now.")
    if source == "minecraft":
        if character_present:
            return Handoff(tool, "already_here", "I’m here in your Minecraft world :3")
        return Handoff(tool, "bridge_only", "We’re chatting through Minecraft. I’m waiting for the game to confirm my character is here.")
    if availability(target) == "enabled":
        return Handoff(tool, "bridge_only", "Open Minecraft with the Super-Bot mod and message me in-game. The bridge is enabled, and the mod handles bringing me into your world :3")
    return Handoff(tool, "unverified", "I can’t verify that the Minecraft bridge is ready right now.")


def live_availability(target):
    from Ai.developer_debug import managed_telemetry
    state = managed_telemetry()
    return state.get("discord" if target == "discord" else "minecraft_bridge", "unknown")
