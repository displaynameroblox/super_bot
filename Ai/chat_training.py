"""Opt-in local storage and export of chat exchanges for later fine-tuning."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path


def training_enabled() -> bool:
    return os.getenv("SUPER_BOT_COLLECT_CHAT_TRAINING", "").strip() == "1"


def database_path() -> Path:
    configured = os.getenv("SUPER_BOT_CHAT_TRAINING_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif os.uname().sysname == "Darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "super_bot" / "training" / "chat-exchanges.sqlite"


def save_exchange(database: Path, user_message: str, assistant_message: str) -> None:
    if not user_message.strip() or not assistant_message.strip():
        raise ValueError("Both chat-training messages must be non-empty.")
    database.parent.mkdir(parents=True, exist_ok=True)
    if not database.exists():
        database.touch(mode=0o600)
    database.chmod(stat.S_IRUSR | stat.S_IWUSR)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS exchanges (
                id INTEGER PRIMARY KEY,
                messages_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO exchanges(messages_json, created_at) VALUES (?, ?)",
            (
                json.dumps(
                    [
                        {"role": "user", "content": user_message},
                        {"role": "assistant", "content": assistant_message},
                    ],
                    ensure_ascii=False,
                ),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
    database.chmod(stat.S_IRUSR | stat.S_IWUSR)


def export_exchanges(database: Path, output: Path) -> int:
    if not database.is_file():
        raise FileNotFoundError(f"No opt-in chat training database exists at {database}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT messages_json FROM exchanges ORDER BY id"
        ).fetchall()
    if not rows:
        raise ValueError(f"No chat exchanges are available in {database}")

    output.chmod(stat.S_IRUSR | stat.S_IWUSR) if output.exists() else None
    with output.open("w", encoding="utf-8") as destination:
        for (messages_json,) in rows:
            messages = json.loads(messages_json)
            if (
                not isinstance(messages, list)
                or len(messages) != 2
                or any(
                    not isinstance(message, dict)
                    or message.get("role") not in {"user", "assistant"}
                    or not isinstance(message.get("content"), str)
                    or not message["content"].strip()
                    for message in messages
                )
            ):
                raise ValueError(f"Invalid chat exchange in {database}")
            destination.write(
                json.dumps({"messages": messages}, ensure_ascii=False) + "\n"
            )
    output.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return len(rows)
