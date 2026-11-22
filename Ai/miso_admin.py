"""Local provisioning and explicit, repeatable legacy Miso imports."""
from __future__ import annotations

import argparse
import getpass
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from Ai import accounts, backup, database, miso_roblox
from Ai.miso_memory import DEVICE_SCHEMA, SCHEMA, AccessDenied, DeviceMemory, MemoryStore


def readonly(path):
    return closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15))


def enable(path):
    path = Path(path).expanduser()
    with readonly(path) as source:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"client_messages", "chat_sessions", "auth_tokens"} <= tables:
            raise ValueError("Choose an existing Super-Bot TUI client.db.")
    if not {"miso_devices", "miso_roblox_grants", "miso_roblox_requests"} <= tables:
        backup.create_backup(path, label="before-miso-memory", keep=0)
    db = database.Database(path)
    with closing(db._open()) as conn:
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise ValueError("Shared Miso memory requires SQLite WAL.")
    with db.connection() as conn:
        for statement in database._split_statements(SCHEMA + DEVICE_SCHEMA + miso_roblox.SCHEMA):
            conn.execute(statement)
    return db


def checked_import(conn, profile_id, source, external, thread, prompt, reply, timestamp):
    claim = conn.execute(
        "SELECT profile_id FROM miso_imports WHERE source=? AND external_id=?", (source, external)
    ).fetchone()
    if claim:
        if claim["profile_id"] != profile_id:
            raise AccessDenied("These records were already imported by another profile.")
        stored = conn.execute("""
            SELECT thread_id,prompt,reply,created_at FROM miso_turns
            WHERE profile_id=? AND source=? AND external_id=?
        """, (profile_id, source, external)).fetchone()
        if stored is None or tuple(stored) != (thread, prompt, reply, timestamp):
            raise ValueError("A source record changed since the previous import.")
        return 0
    conn.execute("INSERT INTO miso_imports VALUES (?,?,?)", (source, external, profile_id))
    return MemoryStore._insert(conn, profile_id, source, external, thread, prompt, reply, timestamp)


def import_tui(memory, token, session_ids, *, adopt_unowned=False):
    if not session_ids:
        return 0, 0
    backup.create_backup(memory.db.path, label="before-miso-tui-import", keep=0)
    with memory.db.connection() as conn:
        profile = memory._account_profile(conn, token, create=True)
        owned = profile["user_id"]
        inserted = total = 0
        for sid in dict.fromkeys(session_ids):
            row = conn.execute("""
                SELECT s.*, m.messages_json FROM chat_sessions s
                LEFT JOIN client_messages m ON m.session_id=s.id
                WHERE s.id=? AND (s.owner_user_id=? OR (? AND s.owner_user_id IS NULL))
            """, (sid, owned, adopt_unowned)).fetchone()
            if row is None:
                raise AccessDenied("A selected chat is missing or owned by another account.")
            # Import the exact transcript as an archived record. Keep roles and
            # order from structured messages where those are available.
            events = json.loads(row["messages_json"] or "[]")
            for index, message in enumerate(events):
                role, content = message["role"], message["content"]
                if role not in {"user", "assistant"} or not isinstance(content, str):
                    raise ValueError("The source contains an invalid structured message.")
                inserted += checked_import(
                    conn, profile["id"], "tui-legacy", f"{sid}:message:{index}", str(sid),
                    content if role == "user" else "", content if role == "assistant" else "", None,
                )
                total += 1
            inserted += checked_import(conn, profile["id"], "tui-legacy", f"{sid}:transcript", str(sid),
                                       row["chat_log"], "", row["created_at"])
            total += 1
        return total, inserted


def import_discord(memory, token, path):
    path = Path(path).expanduser()
    backup.create_backup(memory.db.path, label="before-miso-discord-import", keep=0)
    with readonly(path) as source, memory.db.connection() as conn:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        owner = source.execute("SELECT user_id FROM owner WHERE singleton=1").fetchone()
        profile = memory._account_profile(conn, token)
        if owner is None or profile is None or profile["discord_id"] != owner["user_id"]:
            raise AccessDenied("Link this account to the owner of the source Discord database first.")
        rows = source.execute("""
            SELECT id,prompt,reply FROM history
            WHERE scope_type='dm' AND scope_id=? AND user_id=? AND channel_id=''
            ORDER BY id
        """, (profile["discord_id"], profile["discord_id"])).fetchall()
        inserted = 0
        for row in rows:
            inserted += checked_import(conn, profile["id"], "discord-legacy",
                                       f"{profile['discord_id']}:{row['id']}", "dm", row["prompt"], row["reply"], None)
        total = len(rows)
        saved = source.execute(
            "SELECT saved_history FROM profiles WHERE user_id=? AND imported=1", (profile["discord_id"],)
        ).fetchone()
        if saved and saved[0]:
            inserted += checked_import(conn, profile["id"], "discord-profile-history", profile["discord_id"],
                                       "profile", saved[0], "", None)
            total += 1
        for row in source.execute(
            "SELECT passage_id,content FROM memories WHERE user_id=? ORDER BY passage_id", (profile["discord_id"],)
        ):
            inserted += checked_import(conn, profile["id"], "discord-knowledge",
                                       f"{profile['discord_id']}:{row['passage_id']}", "knowledge", row["content"], "", None)
            total += 1
        return total, inserted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Existing TUI client.db")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("enable")
    register = commands.add_parser("register")
    register.add_argument("username")
    tui = commands.add_parser("import-tui")
    tui.add_argument("username")
    tui.add_argument("session_ids", nargs="+", type=int)
    tui.add_argument("--adopt-unowned", action="store_true")
    discord = commands.add_parser("import-discord")
    discord.add_argument("username")
    discord.add_argument("source", type=Path)
    issue = commands.add_parser("issue-device")
    issue.add_argument("username")
    issue.add_argument("label")
    issue.add_argument("--scope", choices=("read", "read-write"), default="read")
    revoke = commands.add_parser("revoke-device")
    revoke.add_argument("username")
    revoke.add_argument("device_id")
    commands.add_parser("list-devices").add_argument("username")
    commands.add_parser("revoke-all-devices").add_argument("username")
    roblox = commands.add_parser("issue-roblox", help="issue a seven-day, owner-only chat grant")
    roblox.add_argument("username")
    roblox.add_argument("universe_id")
    roblox.add_argument("player_id")
    roblox_revoke = commands.add_parser("revoke-roblox")
    roblox_revoke.add_argument("username")
    roblox_revoke.add_argument("grant_id")
    commands.add_parser("share-roblox-note").add_argument("username")
    args = parser.parse_args()
    try:
        db = enable(args.db)
        if args.action == "enable":
            print("Shared memory enabled.")
            return 0
        account_service = accounts.AccountService(db)
        if args.action == "register":
            account_service.register(args.username, getpass.getpass("New local account password: "))
            print("Local account registered.")
            return 0
        _, token = account_service.login(args.username, getpass.getpass("Local account password: "))
        try:
            memory = DeviceMemory(db)
            if args.action == "import-tui":
                total, inserted = import_tui(memory, token, args.session_ids, adopt_unowned=args.adopt_unowned)
            elif args.action == "import-discord":
                total, inserted = import_discord(memory, token, args.source)
            elif args.action == "issue-device":
                import sys
                if not sys.stdout.isatty():
                    raise ValueError("Issue device credentials in a private interactive terminal.")
                device_id, secret = memory.issue_device(token, args.label, args.scope)
                print(f"Device ID {device_id}\nCopy this bearer token to the device credential store: {secret}")
                return 0
            elif args.action == "issue-roblox":
                import sys
                if not sys.stdout.isatty():
                    raise ValueError("Issue Roblox credentials in a private interactive terminal.")
                grant_id, secret = miso_roblox.RobloxMemory(db).issue(token, args.universe_id, args.player_id)
                print(f"Grant ID {grant_id}\nCopy this bearer token to the Roblox experience secret store: {secret}")
                return 0
            elif args.action == "revoke-roblox":
                miso_roblox.RobloxMemory(db).revoke(token, args.grant_id)
                print("Roblox grant revoked.")
                return 0
            elif args.action == "share-roblox-note":
                note = getpass.getpass("Note for Roblox and your linked private Miso clients: ")
                miso_roblox.RobloxMemory(db).publish_note(token, note)
                print("Shared note saved.")
                return 0
            elif args.action == "list-devices":
                for device in memory.list_devices(token):
                    print(f"{device['id']}  {device['scope']}  {device['label']}")
                return 0
            else:
                memory.revoke_device(token, getattr(args, "device_id", None))
                print("Device access revoked.")
                return 0
            print(f"Validated {total} records. Imported {inserted} new records.")
        finally:
            account_service.logout(token)
    except (ValueError, accounts.AccountError) as exc:
        parser.exit(1, f"{exc}\n")
    except (OSError, sqlite3.Error, database.DatabaseError, backup.BackupError):
        parser.exit(1, "Could not access or validate the Miso database. No import was committed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
