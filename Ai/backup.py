"""Local backup and restore for the Super Bot database."""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from Ai import database

BACKUP_SUFFIX = ".db"
KEEP_BACKUPS = 10
_LABEL = re.compile(r"[^A-Za-z0-9_.-]+")


class BackupError(RuntimeError):
    """A backup or restore could not be completed; the live database is untouched."""


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    size: int
    modified: float


def backup_directory(db_path: Path) -> Path:
    return Path(db_path).expanduser().parent / "backups"


def _check_database_file(path: Path) -> int:
    """Validate a SQLite file and return its schema version."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            problems = database.integrity_problems(connection)
            if problems:
                raise BackupError(f"{path.name} failed its integrity check: {problems[0]}")
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"users", "chat_sessions"} <= tables:
                raise BackupError(f"{path.name} is not a Super Bot database.")
            return database.schema_version(connection)
    except sqlite3.Error as exc:
        raise BackupError(f"{path.name} is not a readable database: {exc}") from exc


def create_backup(
    db_path: Path | str,
    destination: Path | str | None = None,
    *,
    label: str = "manual",
    keep: int = KEEP_BACKUPS,
) -> Path:
    """Write a consistent, verified snapshot (safe while the server is running)."""
    db_path = Path(db_path).expanduser()
    if not db_path.is_file():
        raise BackupError(f"No database found at {db_path}.")
    directory = Path(destination).expanduser() if destination else backup_directory(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    database.restrict_permissions(directory, directory=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_label = _LABEL.sub("-", label).strip("-") or "backup"
    target = directory / f"super_bot-{stamp}-{safe_label}{BACKUP_SUFFIX}"
    counter = 1
    while target.exists():
        counter += 1
        target = directory / f"super_bot-{stamp}-{safe_label}-{counter}{BACKUP_SUFFIX}"
    partial = target.with_name(target.name + ".partial")
    try:
        with contextlib.closing(sqlite3.connect(db_path)) as source, contextlib.closing(
            sqlite3.connect(partial)
        ) as snapshot:
            source.backup(snapshot)
        _check_database_file(partial)
        database.restrict_permissions(partial)
        os.replace(partial, target)
    except (sqlite3.Error, BackupError, OSError) as exc:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise BackupError(f"Backup failed: {exc}") from exc
    if keep > 0 and destination is None:
        for old in list_backups(db_path)[keep:]:
            with contextlib.suppress(OSError):
                old.path.unlink()
    return target


def list_backups(db_path: Path | str, directory: Path | str | None = None) -> list[BackupInfo]:
    folder = Path(directory).expanduser() if directory else backup_directory(Path(db_path))
    if not folder.is_dir():
        return []
    infos = [
        BackupInfo(path, path.stat().st_size, path.stat().st_mtime)
        for path in folder.glob(f"super_bot-*{BACKUP_SUFFIX}")
        if path.is_file()
    ]
    return sorted(infos, key=lambda info: (info.modified, info.path.name), reverse=True)


def restore_backup(backup_path: Path | str, db_path: Path | str) -> Path | None:
    """Replace the live database with a verified backup.

    Stop the server first. The backup is validated and staged next to the live
    file, the current data is saved as a "pre-restore" backup, and the swap is a
    single atomic rename, so an interrupted restore never leaves a half-written
    database. Returns the pre-restore backup path (None if there was no database).
    """
    backup_path = Path(backup_path).expanduser()
    db_path = Path(db_path).expanduser()
    if not backup_path.is_file():
        raise BackupError(f"Backup file not found: {backup_path}")
    if _check_database_file(backup_path) > database.LATEST_VERSION:
        raise BackupError(
            "This backup was made by a newer Super Bot. Update Super Bot before restoring it."
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    staged = db_path.with_name(db_path.name + ".restoring")
    try:
        with contextlib.closing(sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)) as source, contextlib.closing(
            sqlite3.connect(staged)
        ) as copy:
            source.backup(copy)
        _check_database_file(staged)
    except (sqlite3.Error, BackupError, OSError) as exc:
        with contextlib.suppress(OSError):
            staged.unlink()
        raise BackupError(f"Restore failed; your current data was not changed: {exc}") from exc
    try:
        previous = create_backup(db_path, label="pre-restore") if db_path.is_file() else None
        database.restrict_permissions(staged)
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(OSError):
                db_path.with_name(db_path.name + suffix).unlink()
        os.replace(staged, db_path)
    except (OSError, BackupError) as exc:
        with contextlib.suppress(OSError):
            staged.unlink()
        raise BackupError(f"Restore failed; your current data was not changed: {exc}") from exc
    return previous
