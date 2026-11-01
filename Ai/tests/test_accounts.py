import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from Ai import accounts, auth, backup, database, profile_store, server, session_store


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class TempDatabaseCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "data" / "super_bot.db"
        self.clock = Clock()
        self.db = database.Database(self.path)
        self.accounts = accounts.AccountService(self.db, clock=self.clock)
        self.profiles = profile_store.ProfileStore(self.db, clock=self.clock)
        self.sessions = session_store.SessionStore(self.db, clock=self.clock)

    def save_chat(self, session_id: int, owner: int | None, log: str = "hello") -> None:
        self.sessions.save(
            session_id,
            chat_log=log,
            ai_persona="p",
            plr_name="Alex",
            ai_style="",
            ai_skills={},
            password_verifier=None,
            host_token_hash=None,
            owner_user_id=owner,
        )


class PasswordTests(unittest.TestCase):
    def test_hashes_are_salted_verifiable_and_never_plaintext(self) -> None:
        for prefer_argon2 in (True, False):
            with self.subTest(prefer_argon2=prefer_argon2):
                first = auth.hash_password("correct horse", prefer_argon2=prefer_argon2)
                second = auth.hash_password("correct horse", prefer_argon2=prefer_argon2)
                self.assertNotEqual(first, second)
                self.assertNotIn("correct horse", first)
                self.assertTrue(auth.verify_password("correct horse", first))
                self.assertFalse(auth.verify_password("wrong horse", first))

    def test_malformed_hashes_never_verify(self) -> None:
        for stored in ("", "plaintext", "scrypt$1$2", "scrypt$x$y$z$a$b"):
            self.assertFalse(auth.verify_password("anything", stored))

    def test_credential_rules(self) -> None:
        for bad in ("ab", "has space", "x" * 33, "bad/name"):
            with self.assertRaises(auth.InvalidCredentials):
                auth.validate_username(bad)
        with self.assertRaises(auth.InvalidCredentials):
            auth.validate_password("short")
        with self.assertRaises(auth.InvalidCredentials):
            auth.validate_password("x" * 300)


class AccountTests(TempDatabaseCase):
    def test_register_login_logout(self) -> None:
        user = self.accounts.register("Alice", "password-one")
        _, token = self.accounts.login("alice", "password-one")
        self.assertEqual(self.accounts.authenticate(token).id, user.id)
        self.accounts.logout(token)
        self.assertIsNone(self.accounts.authenticate(token))

    def test_usernames_are_unique_ignoring_case(self) -> None:
        self.accounts.register("Alice", "password-one")
        with self.assertRaises(accounts.AccountError) as caught:
            self.accounts.register("aLICE", "password-two")
        self.assertEqual(caught.exception.status, 409)

    def test_wrong_credentials_are_rejected_without_revealing_which_part(self) -> None:
        self.accounts.register("alice", "password-one")
        for username, password in (("alice", "nope-nope"), ("ghost", "password-one")):
            with self.assertRaises(accounts.AccountError) as caught:
                self.accounts.login(username, password)
            self.assertEqual(str(caught.exception), "Incorrect username or password.")

    def test_tokens_are_stored_only_as_hashes(self) -> None:
        self.accounts.register("alice", "password-one")
        _, token = self.accounts.login("alice", "password-one")
        with self.db.connection() as db:
            stored = db.execute("SELECT token_hash FROM auth_tokens").fetchone()[0]
            dumped = b"".join(bytes(str(tuple(r)), "utf-8") for r in db.execute("SELECT * FROM users"))
        self.assertNotEqual(bytes(stored), token.encode())
        self.assertNotIn(b"password-one", dumped)

    def test_sessions_expire(self) -> None:
        self.accounts.register("alice", "password-one")
        _, token = self.accounts.login("alice", "password-one")
        self.clock.now += auth.TOKEN_TTL_SECONDS - 1
        self.assertIsNotNone(self.accounts.authenticate(token))
        self.clock.now += 2
        self.assertIsNone(self.accounts.authenticate(token))

    def test_all_sessions_can_be_invalidated(self) -> None:
        self.accounts.register("alice", "password-one")
        tokens = [self.accounts.login("alice", "password-one")[1] for _ in range(3)]
        self.assertEqual(self.accounts.logout_everywhere(self.accounts.authenticate(tokens[0]).id), 3)
        self.assertTrue(all(self.accounts.authenticate(t) is None for t in tokens))

    def test_password_change_requires_the_old_password_and_ends_other_sessions(self) -> None:
        user = self.accounts.register("alice", "password-one")
        _, current = self.accounts.login("alice", "password-one")
        _, other = self.accounts.login("alice", "password-one")
        with self.assertRaises(accounts.AccountError):
            self.accounts.change_password(user.id, "wrong-wrong", "password-two")
        self.accounts.change_password(user.id, "password-one", "password-two", keep_token=current)
        self.assertIsNotNone(self.accounts.authenticate(current))
        self.assertIsNone(self.accounts.authenticate(other))
        with self.assertRaises(accounts.AccountError):
            self.accounts.login("alice", "password-one")
        self.accounts.login("alice", "password-two")

    def test_repeated_failures_are_rate_limited_and_recover_with_time(self) -> None:
        self.accounts.register("alice", "password-one")
        for _ in range(accounts.MAX_FAILED_ATTEMPTS):
            with self.assertRaises(accounts.AccountError):
                self.accounts.login("alice", "bad-password")
        with self.assertRaises(accounts.AccountError) as caught:
            self.accounts.login("alice", "password-one")
        self.assertEqual(caught.exception.status, 429)
        self.clock.now += accounts.LOCKOUT_WINDOW_SECONDS + 1
        self.accounts.login("alice", "password-one")

    def test_delete_account_removes_profile_tokens_and_chats_only_for_that_user(self) -> None:
        alice = self.accounts.register("alice", "password-one")
        bob = self.accounts.register("bob", "password-two")
        self.save_chat(1, alice.id)
        self.save_chat(2, bob.id)
        self.accounts.delete_account(alice.id, "password-one")
        self.assertIsNone(self.sessions.get_for_user(alice.id, 1))
        self.assertIsNotNone(self.sessions.get_for_user(bob.id, 2))
        with self.assertRaises(profile_store.ProfileError):
            self.profiles.get(alice.id)

    def test_recovery_code_resets_the_password_once(self) -> None:
        user = self.accounts.register("alice", "password-one")
        _, token = self.accounts.login("alice", "password-one")
        code = self.accounts.create_recovery_code(user.id, "password-one")
        with self.assertRaises(accounts.AccountError):
            self.accounts.recover("alice", "00000-00000-00000-00000", "password-new")
        self.accounts.recover("alice", code.lower(), "password-new")
        self.assertIsNone(self.accounts.authenticate(token))
        self.accounts.login("alice", "password-new")
        with self.assertRaises(accounts.AccountError):
            self.accounts.recover("alice", code, "password-again")


class IsolationTests(TempDatabaseCase):
    def test_one_user_cannot_read_modify_or_delete_anothers_data(self) -> None:
        alice = self.accounts.register("alice", "password-one")
        bob = self.accounts.register("bob", "password-two")
        self.save_chat(10, alice.id, "alice secret")
        self.profiles.update(alice.id, preferences={"theme": "dark"})

        self.assertEqual([c["session_id"] for c in self.sessions.list_for_user(alice.id)], [10])
        self.assertEqual(self.sessions.list_for_user(bob.id), [])
        self.assertEqual(self.sessions.list_for_user(bob.id, "secret"), [])
        self.assertIsNone(self.sessions.get_for_user(bob.id, 10))
        self.assertFalse(self.sessions.delete_for_user(bob.id, 10))
        self.assertIsNotNone(self.sessions.get_for_user(alice.id, 10))
        self.assertEqual(self.profiles.get(bob.id)["preferences"], {})

    def test_updating_a_session_never_changes_its_owner(self) -> None:
        alice = self.accounts.register("alice", "password-one")
        bob = self.accounts.register("bob", "password-two")
        self.save_chat(10, alice.id)
        self.save_chat(10, bob.id, "taken over?")
        self.assertEqual(self.sessions.owner_of(10), alice.id)

    def test_search_treats_wildcards_literally(self) -> None:
        alice = self.accounts.register("alice", "password-one")
        self.save_chat(1, alice.id, "100% sure")
        self.save_chat(2, alice.id, "plain text")
        self.assertEqual([c["session_id"] for c in self.sessions.list_for_user(alice.id, "%")], [1])


class ProfileTests(TempDatabaseCase):
    def test_profile_updates_validate_and_reject_credentials(self) -> None:
        user = self.accounts.register("alice", "password-one", display_name="Alice")
        profile = self.profiles.update(
            user.id, display_name=" Al ", preferences={"theme": "dark"}, model_settings={"model": "x"}
        )
        self.assertEqual(profile["display_name"], "Al")
        for bad in (
            {"model_settings": {"gemini_api_key": "secret"}},
            {"model_settings": {"nested": {"Token": "x"}}},
            {"display_name": ""},
            {"display_name": "x" * 65},
            {"avatar": "bad\x00"},
            {"preferences": {"big": "x" * 20000}},
            {"preferences": []},
        ):
            with self.subTest(bad=bad), self.assertRaises(profile_store.ProfileError):
                self.profiles.update(user.id, **bad)
        self.assertEqual(self.profiles.get(user.id)["display_name"], "Al")


class DatabaseTests(TempDatabaseCase):
    def test_data_survives_reopening_and_files_are_private(self) -> None:
        user = self.accounts.register("alice", "password-one")
        self.save_chat(5, user.id)
        reopened = database.Database(self.path)
        self.assertEqual(
            session_store.SessionStore(reopened).get_for_user(user.id, 5)["chat_log"], "hello"
        )
        self.assertEqual(database.schema_version(sqlite3.connect(self.path)), database.LATEST_VERSION)
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o077, 0)
            self.assertEqual(self.path.parent.stat().st_mode & 0o077, 0)

    def test_failed_writes_roll_back(self) -> None:
        with self.assertRaises(RuntimeError), self.db.connection() as db:
            db.execute(
                "INSERT INTO users (username, password_hash, created_at, updated_at) "
                "VALUES ('x', 'h', 0, 0)"
            )
            raise RuntimeError("boom")
        self.assertEqual(self.accounts.user_count(), 0)

    def test_pending_migrations_run_after_an_automatic_backup(self) -> None:
        self.accounts.register("alice", "password-one")
        new_migration = (database.LATEST_VERSION + 1, "CREATE TABLE extra (id INTEGER);")
        with patch.object(database, "MIGRATIONS", database.MIGRATIONS + (new_migration,)), patch.object(
            database, "LATEST_VERSION", new_migration[0]
        ):
            database.Database(self.path)
            with sqlite3.connect(self.path) as connection:
                self.assertEqual(database.schema_version(connection), new_migration[0])
                connection.execute("SELECT * FROM extra")
        names = [info.path.name for info in backup.list_backups(self.path)]
        self.assertTrue(any("pre-migration-v1" in name for name in names))
        self.assertEqual(self.accounts.user_count(), 1)

    def test_a_failed_migration_changes_nothing(self) -> None:
        broken = (database.LATEST_VERSION + 1, "CREATE TABLE ok (id INTEGER);\nSELECT * FROM missing;")
        with patch.object(database, "MIGRATIONS", database.MIGRATIONS + (broken,)), patch.object(
            database, "LATEST_VERSION", broken[0]
        ):
            with self.assertRaises(database.DatabaseError):
                database.Database(self.path, backup_before_migrate=False)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(database.schema_version(connection), database.LATEST_VERSION - 0)
            self.assertIsNone(
                connection.execute("SELECT name FROM sqlite_master WHERE name='ok'").fetchone()
            )

    def test_a_newer_database_is_refused_with_guidance(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version = 999")
        with self.assertRaisesRegex(database.DatabaseError, "Update Super Bot"):
            database.Database(self.path)

    def test_a_corrupt_database_is_reported_not_overwritten(self) -> None:
        self.path.write_bytes(b"this is definitely not sqlite" * 100)
        with self.assertRaisesRegex(database.DatabaseError, "restore"):
            database.Database(self.path)
        self.assertTrue(self.path.read_bytes().startswith(b"this is"))


class BackupTests(TempDatabaseCase):
    def test_backup_and_restore_round_trip(self) -> None:
        user = self.accounts.register("alice", "password-one")
        self.save_chat(1, user.id, "before")
        snapshot = backup.create_backup(self.path)
        self.save_chat(1, user.id, "after")
        self.accounts.register("bob", "password-two")

        previous = backup.restore_backup(snapshot, self.path)

        restored = database.Database(self.path)
        self.assertEqual(session_store.SessionStore(restored).get_for_user(user.id, 1)["chat_log"], "before")
        self.assertEqual(accounts.AccountService(restored).user_count(), 1)
        self.assertIsNotNone(previous)
        self.assertEqual(
            database.Database(previous, backup_before_migrate=False).check_integrity(), []
        )

    def test_a_bad_backup_is_rejected_and_live_data_is_untouched(self) -> None:
        self.accounts.register("alice", "password-one")
        for content in (b"garbage", b""):
            bogus = self.directory / "bogus.db"
            bogus.write_bytes(content)
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(bogus, self.path)
        unrelated = self.directory / "other.db"
        with sqlite3.connect(unrelated) as connection:
            connection.execute("CREATE TABLE t (x)")
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(unrelated, self.path)
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(self.directory / "missing.db", self.path)
        self.assertEqual(self.accounts.user_count(), 1)
        self.assertFalse(list(self.path.parent.glob("*.restoring")))

    def test_an_interrupted_restore_leaves_the_live_database_intact(self) -> None:
        self.accounts.register("alice", "password-one")
        snapshot = backup.create_backup(self.path)
        self.accounts.register("bob", "password-two")
        with patch.object(backup.os, "replace", side_effect=OSError("disk unplugged")):
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(snapshot, self.path)
        self.assertEqual(self.accounts.user_count(), 2)
        self.assertEqual(database.Database(self.path).check_integrity(), [])
        self.assertFalse(list(self.path.parent.glob("*.restoring")))

    def test_a_backup_from_a_newer_version_is_refused(self) -> None:
        snapshot = backup.create_backup(self.path)
        with sqlite3.connect(snapshot) as connection:
            connection.execute("PRAGMA user_version = 999")
        with self.assertRaisesRegex(backup.BackupError, "newer"):
            backup.restore_backup(snapshot, self.path)

    def test_old_automatic_backups_are_pruned(self) -> None:
        for index in range(4):
            backup.create_backup(self.path, label=f"n{index}", keep=2)
        self.assertEqual(len(backup.list_backups(self.path)), 2)


class AccountApiTests(TempDatabaseCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch.multiple(server, account_service=None, profiles=None, store=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        for state in (server.chat_sessions, server.session_tokens, server.session_token_expiry):
            self.addCleanup(state.clear)
        server.init_persistence(self.path)
        self.client = TestClient(server.app)

    def register(self, name: str) -> dict[str, str]:
        response = self.client.post(
            "/accounts/register", json={"username": name, "password": f"password-{name}"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def test_accounts_are_unavailable_until_persistence_is_enabled(self) -> None:
        server.account_service = server.profiles = server.store = None
        self.assertEqual(
            self.client.post("/accounts/login", json={"username": "a", "password": "b"}).status_code,
            503,
        )

    def test_full_account_flow_and_token_validation(self) -> None:
        headers = self.register("alice")
        me = self.client.get("/accounts/me", headers=headers).json()
        self.assertEqual(me["username"], "alice")
        self.assertEqual(self.client.get("/accounts/me").status_code, 401)
        self.assertEqual(
            self.client.get("/accounts/me", headers={"Authorization": "Bearer nope"}).status_code, 401
        )
        updated = self.client.post(
            "/accounts/profile", headers=headers, json={"display_name": "Al"}
        ).json()
        self.assertEqual(updated["display_name"], "Al")
        leak = self.client.post(
            "/accounts/profile", headers=headers, json={"model_settings": {"api_key": "x"}}
        )
        self.assertEqual(leak.status_code, 400)
        self.assertEqual(self.client.post("/accounts/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/accounts/me", headers=headers).status_code, 401)

    def test_duplicate_registration_and_login_errors(self) -> None:
        self.register("alice")
        again = self.client.post(
            "/accounts/register", json={"username": "ALICE", "password": "password-other"}
        )
        self.assertEqual(again.status_code, 409)
        bad = self.client.post("/accounts/login", json={"username": "alice", "password": "x" * 9})
        self.assertEqual(bad.status_code, 401)

    def test_registration_can_be_closed_after_the_first_account(self) -> None:
        with patch.dict(os.environ, {"SUPER_BOT_ALLOW_REGISTRATION": "0"}):
            self.register("alice")
            second = self.client.post(
                "/accounts/register", json={"username": "bob", "password": "password-bob"}
            )
        self.assertEqual(second.status_code, 403)

    def test_chats_belong_to_the_account_that_created_them(self) -> None:
        alice, bob = self.register("alice"), self.register("bob")
        server.chat_sessions[7] = server.Chatsession(
            Chat_log="hi there", AI_persona="p", plr_name="Alice", session_ID=7
        )
        server.persist_session(7, owner_user_id=server.account_service.authenticate(
            alice["Authorization"].split()[1]).id)
        self.assertEqual(
            [c["session_id"] for c in self.client.post("/accounts/chats", headers=alice, json={}).json()["chats"]],
            [7],
        )
        self.assertEqual(self.client.post("/accounts/chats", headers=bob, json={}).json()["chats"], [])
        self.assertEqual(
            self.client.post("/accounts/chats/get", headers=bob, json={"session_id": 7}).status_code, 404
        )
        self.assertEqual(
            self.client.post("/accounts/chats/delete", headers=bob, json={"session_id": 7}).status_code, 404
        )
        self.assertIn(7, server.chat_sessions)
        deleted = self.client.post("/accounts/chats/delete", headers=alice, json={"session_id": 7})
        self.assertEqual(deleted.status_code, 200)
        self.assertNotIn(7, server.chat_sessions)

    def test_session_tokens_expire_and_survive_restart(self) -> None:
        server.chat_sessions[9] = server.Chatsession(
            Chat_log="x", AI_persona="p", plr_name="A", session_ID=9
        )
        token = server.issue_access_token(9)
        header = f"Bearer {token}"
        self.assertEqual(server.authorized_session_id(header), 9)
        token_id = server.access_token_id(header)
        server.session_token_expiry[token_id] = 1.0
        self.assertIsNone(server.authorized_session_id(header))
        self.assertNotIn(token_id, server.session_tokens)


if __name__ == "__main__":
    unittest.main()
