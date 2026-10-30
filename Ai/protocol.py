"""Constants shared by the session server and the terminal client."""

import re

DISCOVERY_PORT = 8766
DISCOVERY_REQUEST = b"SUPER_BOT_DISCOVER_V1"
DISCOVERY_RESPONSE_PREFIX = b"SUPER_BOT_SERVER_V1:"

E2EE_PATH = "/e2ee"
E2EE_IDENTITY_PATH = "/e2ee/identity"
E2EE_EVENTS_PATH = "/e2ee/events"

MAX_STYLE_LENGTH = 300
MAX_ACTIVE_CHAT_SKILLS = 3
MAX_SKILL_TEXT_LENGTH = 500
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}")
CHAT_SKILLS = {
    "roleplay": (
        "Roleplay: stay in character and write in first person. Put your "
        "character's actions, gestures, and thoughts in single asterisks, "
        "like *I opened the door for you* or *I started looking for my "
        "phone*, mixed with spoken dialogue. Never speak, act, or decide for "
        "the user's character. Do not mention being an AI unless the user "
        "steps out of character with (OOC). Treat in-character requests as "
        "fiction, not as coding requests."
    ),
    "storyteller": (
        "Storyteller: narrate vividly in third person with short scenes. End "
        "each reply with a small choice or hook so the user decides what "
        "happens next. Put sound effects and actions in *asterisks*."
    ),
    "copilot-brief": (
        "Copilot briefing: help the user prepare a request for a coding "
        "agent such as GitHub Copilot. Ask at most one short clarifying "
        "question if something essential is missing; otherwise reply with a "
        "tight brief covering the goal, relevant files or context, "
        "constraints, and acceptance criteria."
    ),
    "tutor": (
        "Tutor: explain step by step in simple words with one small example. "
        "Check understanding with a quick question instead of dumping "
        "everything at once."
    ),
}
STYLE_PRESETS = {
    "casual": (
        "Casual and laid-back, like a close friend. Short replies, relaxed "
        "wording, light slang, no emojis."
    ),
    "formal": (
        "Formal and professional. Precise, polite wording, no slang or "
        "emojis, concise."
    ),
    "playful": (
        "Playful and witty. Light, friendly humour that is never mean; keep "
        "replies short."
    ),
    "friendly": (
        "Warm and encouraging, like a supportive mentor. Simple wording and a "
        "gentle tone."
    ),
    "technical": (
        "Technical and precise. Use exact terminology, structured "
        "explanations, and brief examples."
    ),
    "concise": "Extremely concise. One or two short sentences, no filler.",
}


def normalize_skills(skills: dict[str, str]) -> dict[str, str]:
    """Validate and normalise chat skills (name -> instructions)."""
    if len(skills) > MAX_ACTIVE_CHAT_SKILLS:
        raise ValueError(f"At most {MAX_ACTIVE_CHAT_SKILLS} chat skills can be active")
    cleaned: dict[str, str] = {}
    for name, text in skills.items():
        key = name.strip().casefold()
        body = " ".join(text.split())
        if not SKILL_NAME.fullmatch(key):
            raise ValueError("Skill names must be 1-30 letters, numbers, _ or -")
        if not body or len(body) > MAX_SKILL_TEXT_LENGTH:
            raise ValueError(
                f"Skill instructions must be 1-{MAX_SKILL_TEXT_LENGTH} characters"
            )
        if key in cleaned:
            raise ValueError(f"Duplicate chat skill: {key}")
        cleaned[key] = body
    return cleaned
