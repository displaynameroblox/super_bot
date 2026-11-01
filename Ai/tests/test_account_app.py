import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from Ai import account_app, gateway, server


def gateway_requester(client: TestClient):
    """Calls the real inner app, but only through routes the encrypted gateway allows."""

    def requester(base_url, method, path, body, token, *, timeout):
        if not any(method == m and p.fullmatch(path) for m, p in gateway.ALLOWED_ROUTES):
            return 404, {"detail": "Not Found"}
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = client.request(method, path, json=body if method != "GET" else None, headers=headers)
        return response.status_code, response.json()

    return requester


class Script:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.output: list[str] = []

    def ask(self, prompt: str) -> str:
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def say(self, text: str) -> None:
        self.output.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.output)


class AccountAppTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.multiple(server, account_service=None, profiles=None, store=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        for state in (server.chat_sessions, server.session_tokens, server.session_token_expiry):
            self.addCleanup(state.clear)
        server.init_persistence(Path(directory.name) / "data" / "super_bot.db")
        reply = patch.object(
            server, "generate_reply",
            side_effect=lambda *a, **k: server.AssistantResponse(reply="Bot says hi", is_coding_request=False),
        )
        reply.start()
        self.addCleanup(reply.stop)
        self.http = TestClient(server.app)

    def app(self, answers: list[str], secrets: list[str] | None = None) -> tuple[account_app.AccountApp, Script]:
        script = Script(answers)
        secret = Script(secrets or [])
        client = account_app.AccountClient("https://127.0.0.1:1", gateway_requester(self.http))
        return account_app.AccountApp(
            client, ask=script.ask, ask_secret=secret.ask, say=script.say
        ), script

    def test_register_chat_and_resume_across_sessions(self) -> None:
        app, script = self.app(
            ["r", "alice", "Alice", "/new", "hello there", "/chats", "/quit"],
            ["password-one", "password-one"],
        )
        self.assertEqual(app.run(), 0)
        self.assertIn("Signed in as Alice", script.text)
        self.assertIn("Bot: Bot says hi", script.text)
        self.assertIn("Bot says hi", script.text)
        self.assertEqual(len(server.store.list_for_user(1)), 1)
        session_id = server.store.list_for_user(1)[0]["session_id"]

        # A fresh app (a new device) signs in and resumes the saved chat.
        app2, script2 = self.app(
            ["l", "alice", f"/open {session_id}", "still there?", "/quit"], ["password-one"]
        )
        self.assertEqual(app2.run(), 0)
        self.assertIn(f"Chat {session_id} opened.", script2.text)
        self.assertIn("You: hello there", script2.text)
        self.assertIn("Bot: Bot says hi", script2.text)
        log = server.chat_sessions[session_id].Chat_log
        self.assertIn("still there?", log)

    def test_other_accounts_cannot_open_or_delete_your_chats(self) -> None:
        alice, _ = self.app(["r", "alice", "", "/new", "/quit"], ["password-one", "password-one"])
        alice.run()
        session_id = server.store.list_for_user(1)[0]["session_id"]
        bob, script = self.app(
            ["r", "bob", "", f"/open {session_id}", f"/delete {session_id}", "y", "/chats", "/quit"],
            ["password-two", "password-two"],
        )
        bob.run()
        self.assertIn("Error: Chat not found", script.text)
        self.assertIn("No chats yet.", script.text)
        self.assertIn(session_id, server.chat_sessions)

    def test_login_failures_are_reported_and_the_app_can_quit(self) -> None:
        app, script = self.app(["l", "nobody", "q"], ["wrong-password"])
        self.assertEqual(app.run(), 0)
        self.assertIn("Error: Incorrect username or password.", script.text)

    def test_mismatched_registration_passwords_are_refused(self) -> None:
        app, script = self.app(["r", "alice", "", "q"], ["password-one", "different-one"])
        app.run()
        self.assertIn("Passwords did not match.", script.text)
        self.assertEqual(server.account_service.user_count(), 0)

    def test_account_management_commands(self) -> None:
        app, script = self.app(
            ["r", "alice", "", "/profile Al", "/recovery", "/password", "/logout"],
            ["password-one", "password-one", "password-one", "password-one", "password-two"],
        )
        app.run()
        self.assertIn("Display name is now Al.", script.text)
        self.assertIn("Recovery code (shown once", script.text)
        self.assertIn("Password changed.", script.text)
        self.assertIn("Signed out.", script.text)
        self.assertEqual(app.client.token, "")

        # The new password works and the old one does not.
        again, again_script = self.app(["l", "alice", "l", "alice", "/quit"], ["password-one", "password-two"])
        again.run()
        self.assertIn("Error: Incorrect username or password.", again_script.text)
        self.assertIn("Signed in as Al", again_script.text)

    def test_delete_account_removes_the_account_and_its_chats(self) -> None:
        app, script = self.app(
            ["r", "alice", "", "/new", "/delete-account", "y"],
            ["password-one", "password-one", "password-one"],
        )
        app.run()
        self.assertIn("Account deleted.", script.text)
        self.assertEqual(server.account_service.user_count(), 0)
        self.assertEqual(server.chat_sessions, {})

    def test_messages_need_an_open_chat_and_respect_the_length_limit(self) -> None:
        app, script = self.app(["r", "alice", "", "hi", "/new", "x" * 1001, "/quit"], ["password-one"] * 2)
        app.run()
        self.assertIn("No chat open.", script.text)
        self.assertIn("at most 1000 characters", script.text)

    def test_log_parsing_handles_multiline_messages(self) -> None:
        log = "[2026-01-01T00:00:00] Al: first\nsecond line\n[2026-01-01T00:00:01] AI: reply"
        self.assertEqual(
            account_app.parse_log(log), [("Al", "first\nsecond line"), ("AI", "reply")]
        )


if __name__ == "__main__":
    unittest.main()
