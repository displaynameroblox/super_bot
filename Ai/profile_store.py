"""Per-account profile, preferences, and model settings."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from Ai.database import Database

MAX_DISPLAY_NAME = 64
MAX_AVATAR = 256
MAX_JSON_BYTES = 16 * 1024
SECRET_KEY_PATTERN = re.compile(r"key|secret|token|password|credential", re.IGNORECASE)
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class ProfileError(ValueError):
    pass


def _clean_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{label} must be text.")
    value = value.strip()
    if len(value) > limit or CONTROL_CHARACTERS.search(value):
        raise ProfileError(f"{label} must be at most {limit} characters without control characters.")
    return value


def _clean_mapping(value: Any, label: str, *, reject_secrets: bool) -> str:
    if not isinstance(value, dict):
        raise ProfileError(f"{label} must be an object.")

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise ProfileError(f"{label} keys must be text.")
                if reject_secrets and SECRET_KEY_PATTERN.search(key):
                    raise ProfileError(
                        f"{label} cannot hold credentials ('{key}'). API keys stay on the server."
                    )
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif not isinstance(node, (str, int, float, bool, type(None))):
            raise ProfileError(f"{label} contains an unsupported value.")

    walk(value)
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ProfileError(f"{label} is larger than {MAX_JSON_BYTES // 1024} KiB.")
    return encoded


class ProfileStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.database = database
        self._clock = clock

    def get(self, user_id: int) -> dict[str, Any]:
        with self.database.connection() as db:
            row = db.execute(
                "SELECT display_name, avatar, preferences_json, model_settings_json "
                "FROM profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise ProfileError("Profile not found.")
        return {
            "display_name": row["display_name"],
            "avatar": row["avatar"],
            "preferences": json.loads(row["preferences_json"]),
            "model_settings": json.loads(row["model_settings_json"]),
        }

    def update(
        self,
        user_id: int,
        *,
        display_name: str | None = None,
        avatar: str | None = None,
        preferences: dict[str, Any] | None = None,
        model_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update only the supplied fields for this user and return the profile."""
        changes: dict[str, Any] = {}
        if display_name is not None:
            name = _clean_text(display_name, "Display name", MAX_DISPLAY_NAME)
            if not name:
                raise ProfileError("Display name cannot be empty.")
            changes["display_name"] = name
        if avatar is not None:
            changes["avatar"] = _clean_text(avatar, "Avatar", MAX_AVATAR)
        if preferences is not None:
            changes["preferences_json"] = _clean_mapping(
                preferences, "Preferences", reject_secrets=False
            )
        if model_settings is not None:
            changes["model_settings_json"] = _clean_mapping(
                model_settings, "Model settings", reject_secrets=True
            )
        if changes:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            with self.database.connection() as db:
                cursor = db.execute(
                    f"UPDATE profiles SET {assignments}, updated_at = ? WHERE user_id = ?",
                    (*changes.values(), self._clock(), user_id),
                )
                if cursor.rowcount == 0:
                    raise ProfileError("Profile not found.")
        return self.get(user_id)
