"""SQLite connection management, schema, and migrations for local Super Bot data."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

DATABASE_FILENAME = "super_bot.db"


class DatabaseError(RuntimeError):
    """Raised with an actionable message when the database cannot be used."""


# Each entry is applied once, in order. Never edit a released migration; append a new one.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            recovery_hash TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE profiles (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            display_name TEXT NOT NULL DEFAULT '',
            avatar TEXT NOT NULL DEFAULT '',
            preferences_json TEXT NOT NULL DEFAULT '{}',
            model_settings_json TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL
        );
        CREATE TABLE auth_tokens (
            token_hash BLOB PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            last_used_at REAL NOT NULL
        );
        CREATE INDEX auth_tokens_user ON auth_tokens(user_id);
        CREATE TABLE chat_sessions (
            id INTEGER PRIMARY KEY,
            owner_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            chat_log TEXT NOT NULL,
            ai_persona TEXT NOT NULL,
            plr_name TEXT NOT NULL,
            ai_style TEXT NOT NULL DEFAULT '',
            ai_skills_json TEXT NOT NULL DEFAULT '{}',
            password_salt BLOB,
            password_digest BLOB,
            host_token_hash BLOB,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX chat_sessions_owner ON chat_sessions(owner_user_id);
        CREATE TABLE session_tokens (
            token_hash BLOB PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            expires_at REAL NOT NULL
        );
        CREATE INDEX session_tokens_session ON session_tokens(session_id);
        """,
    ),
)
LATEST_VERSION = MIGRATIONS[-1][0]


def default_path() -> Path:
    configured = os.getenv("SUPER_BOT_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    from Ai import tls

    return tls.server_data_dir() / DATABASE_FILENAME


def restrict_permissions(path: Path, *, directory: bool = False) -> None:
    """Make a file or directory private to the current user (no-op where unsupported)."""
    if os.name == "nt":
        return
    mode = stat.S_IRWXU if directory else stat.S_IRUSR | stat.S_IWUSR
    with contextlib.suppress(OSError):
        path.chmod(mode)


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def integrity_problems(connection: sqlite3.Connection) -> list[str]:
    """Return a list of problems; an empty list means the database is healthy."""
    try:
        rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        problems = [] if rows == ["ok"] else rows
        problems += [
            f"foreign key violation in {row[0]}"
            for row in connection.execute("PRAGMA foreign_key_check")
        ]
    except sqlite3.DatabaseError as exc:
        return [str(exc)]
    return problems


class Database:
    """Opens private, migrated SQLite connections to one local database file."""

    def __init__(self, path: Path | str, *, backup_before_migrate: bool = True) -> None:
        self.path = Path(path).expanduser()
        self._memory_uri = f"file:superbot-{uuid.uuid4().hex}?mode=memory&cache=shared" if str(path) == ":memory:" else None
        self._anchor = sqlite3.connect(self._memory_uri, uri=True, check_same_thread=False) if self._memory_uri else None
        self._write_lock = threading.RLock()
        self._backup_before_migrate = backup_before_migrate
        self._prepare()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._memory_uri or self.path, uri=bool(self._memory_uri), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def close(self) -> None:
        if getattr(self, "_anchor", None) is not None:
            self._anchor.close()
            self._anchor = None

    def __del__(self):
        self.close()

    def _prepare(self) -> None:
        if self._memory_uri:
            with contextlib.closing(self._open()) as connection:
                self._migrate(connection, 0)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DatabaseError(f"Cannot create the data directory {self.path.parent}: {exc}") from exc
        restrict_permissions(self.path.parent, directory=True)
        existed = self.path.exists()
        try:
            connection = self._open()
        except sqlite3.Error as exc:
            raise DatabaseError(f"Cannot open the database {self.path}: {exc}") from exc
        try:
            if existed:
                problems = integrity_problems(connection)
                if problems:
                    raise DatabaseError(
                        f"The database {self.path} failed its integrity check "
                        f"({problems[0]}). Restore a backup with "
                        "`python -m Ai.local_manager restore-backup`."
                    )
            current = schema_version(connection)
            if current > LATEST_VERSION:
                raise DatabaseError(
                    f"The database {self.path} was created by a newer Super Bot "
                    f"(schema {current}, this version supports {LATEST_VERSION}). "
                    "Update Super Bot instead of opening it with this version."
                )
            if current < LATEST_VERSION:
                if existed and current > 0 and self._backup_before_migrate:
                    from Ai import backup

                    backup.create_backup(self.path, label=f"pre-migration-v{current}")
                journal = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                del journal
                self._migrate(connection, current)
        except sqlite3.DatabaseError as exc:
            raise DatabaseError(f"The database {self.path} is unreadable: {exc}") from exc
        finally:
            connection.close()
        for suffix in ("", "-wal", "-shm"):
            candidate = self.path.with_name(self.path.name + suffix)
            if candidate.exists():
                restrict_permissions(candidate)

    @staticmethod
    def _migrate(connection: sqlite3.Connection, current: int) -> None:
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            try:
                connection.execute("BEGIN IMMEDIATE")
                for statement in _split_statements(script):
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {int(version)}")
                connection.execute("COMMIT")
            except sqlite3.Error:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise

    @contextlib.contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """Read a consistent snapshot without reserving the SQLite writer."""
        guard = self._write_lock if self._memory_uri else contextlib.nullcontext()
        with guard, contextlib.closing(self._open()) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            try:
                yield connection
            finally:
                connection.execute("ROLLBACK")

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection; changes commit on success and roll back on error."""
        with self._write_lock:
            connection = self._open()
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                else:
                    connection.execute("COMMIT")
            finally:
                connection.close()

    def check_integrity(self) -> list[str]:
        with contextlib.closing(self._open()) as connection:
            return integrity_problems(connection)


def _split_statements(script: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines():
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements
