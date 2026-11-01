"""Durable storage for chat sessions, with per-account ownership checks."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from Ai.database import Database


@dataclass
class StoredSession:
    session_id: int
    chat_log: str
    ai_persona: str
    plr_name: str
    ai_style: str
    ai_skills: dict[str, str]
    password_verifier: tuple[bytes, bytes] | None
    host_token_hash: bytes | None
    owner_user_id: int | None


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SessionStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.database = database
        self._clock = clock

    def save(
        self,
        session_id: int,
        *,
        chat_log: str,
        ai_persona: str,
        plr_name: str,
        ai_style: str,
        ai_skills: dict[str, str],
        password_verifier: tuple[bytes, bytes] | None,
        host_token_hash: bytes | None,
        owner_user_id: int | None = None,
    ) -> None:
        """Insert or update a session. The owner is set once, when it is created."""
        now = self._clock()
        salt, digest = password_verifier if password_verifier else (None, None)
        with self.database.connection() as db:
            db.execute(
                "INSERT INTO chat_sessions (id, owner_user_id, chat_log, ai_persona, plr_name, "
                "ai_style, ai_skills_json, password_salt, password_digest, host_token_hash, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET chat_log = excluded.chat_log, "
                "ai_persona = excluded.ai_persona, plr_name = excluded.plr_name, "
                "ai_style = excluded.ai_style, ai_skills_json = excluded.ai_skills_json, "
                "password_salt = excluded.password_salt, "
                "password_digest = excluded.password_digest, "
                "host_token_hash = excluded.host_token_hash, updated_at = excluded.updated_at",
                (
                    session_id,
                    owner_user_id,
                    chat_log,
                    ai_persona,
                    plr_name,
                    ai_style,
                    json.dumps(ai_skills),
                    salt,
                    digest,
                    host_token_hash,
                    now,
                    now,
                ),
            )

    def save_token(self, token_hash: bytes, session_id: int, expires_at: float) -> None:
        with self.database.connection() as db:
            db.execute("DELETE FROM session_tokens WHERE expires_at <= ?", (self._clock(),))
            db.execute(
                "INSERT OR REPLACE INTO session_tokens (token_hash, session_id, expires_at) "
                "VALUES (?, ?, ?)",
                (token_hash, session_id, expires_at),
            )

    def load_all(self) -> tuple[list[StoredSession], dict[bytes, tuple[int, float]]]:
        """Everything the running server needs after a restart (expired tokens dropped)."""
        now = self._clock()
        with self.database.connection() as db:
            db.execute("DELETE FROM session_tokens WHERE expires_at <= ?", (now,))
            rows = db.execute("SELECT * FROM chat_sessions ORDER BY id").fetchall()
            token_rows = db.execute(
                "SELECT token_hash, session_id, expires_at FROM session_tokens"
            ).fetchall()
        sessions = [
            StoredSession(
                row["id"],
                row["chat_log"],
                row["ai_persona"],
                row["plr_name"],
                row["ai_style"],
                json.loads(row["ai_skills_json"]),
                (bytes(row["password_salt"]), bytes(row["password_digest"]))
                if row["password_salt"] is not None
                else None,
                bytes(row["host_token_hash"]) if row["host_token_hash"] is not None else None,
                row["owner_user_id"],
            )
            for row in rows
        ]
        tokens = {
            bytes(row["token_hash"]): (row["session_id"], row["expires_at"]) for row in token_rows
        }
        return sessions, tokens

    # Account-scoped operations: every query is constrained by owner_user_id.

    def list_for_user(self, user_id: int, query: str = "") -> list[dict[str, Any]]:
        sql = (
            "SELECT id, plr_name, ai_persona, updated_at, substr(chat_log, -200) AS preview "
            "FROM chat_sessions WHERE owner_user_id = ?"
        )
        params: list[Any] = [user_id]
        if query.strip():
            sql += " AND chat_log LIKE ? ESCAPE '\\'"
            params.append(f"%{_escape_like(query.strip())}%")
        with self.database.connection() as db:
            rows = db.execute(sql + " ORDER BY updated_at DESC, id DESC", params).fetchall()
        return [
            {
                "session_id": row["id"],
                "player_name": row["plr_name"],
                "persona": row["ai_persona"],
                "updated_at": row["updated_at"],
                "preview": row["preview"],
            }
            for row in rows
        ]

    def get_for_user(self, user_id: int, session_id: int) -> dict[str, Any] | None:
        with self.database.connection() as db:
            row = db.execute(
                "SELECT id, chat_log, ai_persona, plr_name, ai_style FROM chat_sessions "
                "WHERE id = ? AND owner_user_id = ?",
                (session_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": row["id"],
            "chat_log": row["chat_log"],
            "persona": row["ai_persona"],
            "player_name": row["plr_name"],
            "style": row["ai_style"],
        }

    def delete_for_user(self, user_id: int, session_id: int) -> bool:
        with self.database.connection() as db:
            return (
                db.execute(
                    "DELETE FROM chat_sessions WHERE id = ? AND owner_user_id = ?",
                    (session_id, user_id),
                ).rowcount
                > 0
            )

    def owner_of(self, session_id: int) -> int | None:
        with self.database.connection() as db:
            row = db.execute(
                "SELECT owner_user_id FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return row["owner_user_id"] if row else None
