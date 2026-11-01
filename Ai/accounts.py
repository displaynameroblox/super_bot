"""Local account lifecycle: registration, login, logout, and password management."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass

from Ai import auth
from Ai.database import Database

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 300


class AccountError(Exception):
    """An account operation failed; ``status`` suggests an HTTP status code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class User:
    id: int
    username: str
    created_at: float


class _Throttle:
    """Limits repeated failed logins per name to slow down password guessing."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = self._clock() - LOCKOUT_WINDOW_SECONDS
        recent = [stamp for stamp in self._failures.get(key, []) if stamp > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def check(self, key: str) -> None:
        with self._lock:
            if len(self._recent(key)) >= MAX_FAILED_ATTEMPTS:
                raise AccountError(
                    "Too many failed attempts. Wait a few minutes and try again.", 429
                )

    def fail(self, key: str) -> None:
        with self._lock:
            self._recent(key)
            self._failures.setdefault(key, []).append(self._clock())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class AccountService:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.database = database
        self._clock = clock
        self._throttle = _Throttle(clock)
        # Verifying a hash for unknown users keeps timing similar to real logins.
        self._dummy_hash = auth.hash_password("not-a-real-password")

    def register(self, username: str, password: str, *, display_name: str = "") -> User:
        username = auth.validate_username(username)
        auth.validate_password(password)
        password_hash = auth.hash_password(password)
        now = self._clock()
        try:
            with self.database.connection() as db:
                cursor = db.execute(
                    "INSERT INTO users (username, password_hash, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (username, password_hash, now, now),
                )
                db.execute(
                    "INSERT INTO profiles (user_id, display_name, updated_at) VALUES (?, ?, ?)",
                    (cursor.lastrowid, (display_name or username)[:64], now),
                )
                return User(int(cursor.lastrowid), username, now)
        except sqlite3.IntegrityError as exc:
            raise AccountError("That username is already taken.", 409) from exc

    def _issue_token(self, db: sqlite3.Connection, user_id: int) -> str:
        token = auth.new_token()
        now = self._clock()
        db.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (now,))
        db.execute(
            "INSERT INTO auth_tokens (token_hash, user_id, created_at, expires_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (auth.token_digest(token), user_id, now, now + auth.TOKEN_TTL_SECONDS, now),
        )
        return token

    def login(self, username: str, password: str) -> tuple[User, str]:
        key = username.casefold() if isinstance(username, str) else ""
        self._throttle.check(key)
        row = None
        if isinstance(username, str) and isinstance(password, str):
            with self.database.connection() as db:
                row = db.execute(
                    "SELECT id, username, password_hash, created_at FROM users WHERE username = ?",
                    (username,),
                ).fetchone()
        stored = row["password_hash"] if row else self._dummy_hash
        valid = isinstance(password, str) and auth.verify_password(password, stored)
        if row is None or not valid:
            self._throttle.fail(key)
            raise AccountError("Incorrect username or password.", 401)
        self._throttle.reset(key)
        with self.database.connection() as db:
            if auth.needs_rehash(stored):
                db.execute(
                    "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                    (auth.hash_password(password), self._clock(), row["id"]),
                )
            token = self._issue_token(db, row["id"])
        return User(row["id"], row["username"], row["created_at"]), token

    def authenticate(self, token: str | None) -> User | None:
        """Return the user for a valid, unexpired token; otherwise None."""
        if not token or not isinstance(token, str):
            return None
        now = self._clock()
        with self.database.connection() as db:
            row = db.execute(
                "SELECT u.id, u.username, u.created_at, t.expires_at FROM auth_tokens t "
                "JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?",
                (auth.token_digest(token),),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                db.execute(
                    "DELETE FROM auth_tokens WHERE token_hash = ?", (auth.token_digest(token),)
                )
                return None
            db.execute(
                "UPDATE auth_tokens SET last_used_at = ? WHERE token_hash = ?",
                (now, auth.token_digest(token)),
            )
        return User(row["id"], row["username"], row["created_at"])

    def logout(self, token: str) -> None:
        with self.database.connection() as db:
            db.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (auth.token_digest(token),))

    def logout_everywhere(self, user_id: int) -> int:
        """Invalidate every active login for the user; returns how many were removed."""
        with self.database.connection() as db:
            return db.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,)).rowcount

    def _verify_user(self, db: sqlite3.Connection, user_id: int, password: str) -> None:
        key = f"id:{user_id}"
        self._throttle.check(key)
        row = db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None or not isinstance(password, str) or not auth.verify_password(
            password, row["password_hash"]
        ):
            self._throttle.fail(key)
            raise AccountError("Incorrect password.", 401)
        self._throttle.reset(key)

    def change_password(
        self, user_id: int, current_password: str, new_password: str, *, keep_token: str | None = None
    ) -> None:
        auth.validate_password(new_password)
        new_hash = auth.hash_password(new_password)
        with self.database.connection() as db:
            self._verify_user(db, user_id, current_password)
            db.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (new_hash, self._clock(), user_id),
            )
            keep = auth.token_digest(keep_token) if keep_token else b""
            db.execute(
                "DELETE FROM auth_tokens WHERE user_id = ? AND token_hash != ?", (user_id, keep)
            )

    def delete_account(self, user_id: int, password: str) -> None:
        """Delete the account together with its profile, tokens, and chats."""
        with self.database.connection() as db:
            self._verify_user(db, user_id, password)
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def create_recovery_code(self, user_id: int, password: str) -> str:
        """Create a one-time code that can reset the password. Shown only once.

        Anyone holding the code can take over the account, and a lost code cannot
        be recovered: there is no email or cloud service to fall back on.
        """
        code = auth.new_recovery_code()
        with self.database.connection() as db:
            self._verify_user(db, user_id, password)
            db.execute(
                "UPDATE users SET recovery_hash = ? WHERE id = ?",
                (auth.hash_password(auth.normalize_recovery_code(code)), user_id),
            )
        return code

    def recover(self, username: str, recovery_code: str, new_password: str) -> None:
        auth.validate_password(new_password)
        key = f"recover:{str(username).casefold()}"
        self._throttle.check(key)
        with self.database.connection() as db:
            row = db.execute(
                "SELECT id, recovery_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
            stored = row["recovery_hash"] if row and row["recovery_hash"] else self._dummy_hash
            valid = auth.verify_password(
                auth.normalize_recovery_code(str(recovery_code)), stored
            )
            if row is None or not row["recovery_hash"] or not valid:
                self._throttle.fail(key)
                raise AccountError("Invalid username or recovery code.", 401)
            db.execute(
                "UPDATE users SET password_hash = ?, recovery_hash = NULL, updated_at = ? "
                "WHERE id = ?",
                (auth.hash_password(new_password), self._clock(), row["id"]),
            )
            db.execute("DELETE FROM auth_tokens WHERE user_id = ?", (row["id"],))
        self._throttle.reset(key)

    def user_count(self) -> int:
        with self.database.connection() as db:
            return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])
