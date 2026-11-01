import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from Ai import server
from Ai.chat_app import ChatApp, ChatSession, resolve_style
from Ai.protocol import MAX_STYLE_LENGTH, STYLE_PRESETS


class ResolveStyleTests(unittest.TestCase):
    def test_preset_names_resolve_case_insensitively(self) -> None:
        self.assertEqual(resolve_style("Casual"), STYLE_PRESETS["casual"])

    def test_free_text_is_kept_and_whitespace_normalised(self) -> None:
        self.assertEqual(resolve_style("  talk   like a  pirate "), "talk like a pirate")

    def test_off_and_blank_clear_the_style(self) -> None:
        for value in ("", "off", "NONE", "default"):
            with self.subTest(value=value):
                self.assertEqual(resolve_style(value), "")

    def test_overlong_text_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_style("x" * (MAX_STYLE_LENGTH + 1))

    def test_every_preset_fits_the_server_limit(self) -> None:
        for name, text in STYLE_PRESETS.items():
            with self.subTest(name=name):
                self.assertLessEqual(len(text), MAX_STYLE_LENGTH)


class ServerStyleTests(unittest.TestCase):
    def make_session(self, style: str = "") -> server.Chatsession:
        return server.Chatsession(
            Chat_log="",
            AI_persona="A helper.",
            plr_name="Alex",
            session_ID=1,
            AI_style=style,
        )

    def system_prompt_for(self, session: server.Chatsession) -> str:
        reply = '{"reply": "hi", "is_coding_request": false}'
        with patch("Ai.server.phone_model.chat", return_value=reply) as chat:
            server.generate_reply(session, "", current_message="hello")
        return chat.call_args.args[0][0]["content"]

    def test_style_is_added_to_the_system_prompt(self) -> None:
        prompt = self.system_prompt_for(self.make_session("Talk like a pirate."))
        self.assertIn("Talk like a pirate.", prompt)

    def test_prompt_has_no_style_section_when_unset(self) -> None:
        prompt = self.system_prompt_for(self.make_session(""))
        self.assertNotIn("Reply style", prompt)

    def test_training_csvs_are_included_as_personality_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            messages_path = Path(temp_dir) / "messages.csv"
            training_path = Path(temp_dir) / "training.csv"
            with messages_path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=[
                        "conversation_id",
                        "turn_index",
                        "role",
                        "content",
                        "category",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "conversation_id": "warmth",
                        "turn_index": "1",
                        "role": "assistant",
                        "content": "That sounds like a lovely plan.",
                        "category": "casual_warmth",
                    }
                )
            with training_path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=[
                        "conversation_id",
                        "category",
                        "system",
                        "messages_json",
                        "turn_count",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "conversation_id": "warmth",
                        "category": "casual_warmth",
                        "system": "Be natural, warm, and concise.",
                        "messages_json": json.dumps(
                            [{"role": "assistant", "content": "That sounds lovely."}]
                        ),
                        "turn_count": "1",
                    }
                )
            with patch.dict(
                os.environ,
                {
                    "SUPER_BOT_MESSAGES_CSV": str(messages_path),
                    "SUPER_BOT_PERSONALITY_TRAINING_CSV": str(training_path),
                },
            ):
                prompt = self.system_prompt_for(self.make_session(""))

        self.assertIn("Be natural, warm, and concise.", prompt)
        self.assertIn("That sounds like a lovely plan.", prompt)
        self.assertIn("reference only", prompt)

    def test_style_endpoint_requires_a_valid_token(self) -> None:
        request = server.StyleRequest(ses_id=1, style="formal")
        with patch.dict(server.chat_sessions, {1: self.make_session()}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                server.set_style(request, authorization=None)
        self.assertEqual(caught.exception.status_code, 401)

    def test_style_endpoint_updates_and_clears_style(self) -> None:
        session = self.make_session()
        with patch.dict(server.chat_sessions, {1: session}, clear=True), patch.dict(
            server.session_tokens, {}, clear=True
        ):
            token = server.issue_access_token(1)
            header = f"Bearer {token}"
            result = server.set_style(
                server.StyleRequest(ses_id=1, style="  casual  "), authorization=header
            )
            self.assertEqual(result.AI_style, "casual")
            self.assertEqual(session.AI_style, "casual")
            server.set_style(server.StyleRequest(ses_id=1, style=""), authorization=header)
            self.assertEqual(session.AI_style, "")

    def test_token_for_another_session_cannot_change_style(self) -> None:
        with patch.dict(
            server.chat_sessions, {1: self.make_session()}, clear=True
        ), patch.dict(server.session_tokens, {}, clear=True):
            token = server.issue_access_token(2)
            with self.assertRaises(HTTPException) as caught:
                server.set_style(
                    server.StyleRequest(ses_id=1, style="formal"),
                    authorization=f"Bearer {token}",
                )
        self.assertEqual(caught.exception.status_code, 401)


class StyleCommandTests(unittest.TestCase):
    def make_app(self, session: ChatSession | None) -> ChatApp:
        app = ChatApp.__new__(ChatApp)
        app.sessions = [session] if session else []
        app.active_index = 0 if session else -1
        app.base_url = "http://localhost"
        app.executor = Mock()
        app.future = None
        app.pending_kind = ""
        app.status = ""
        return app

    def make_session(self, token: str = "token") -> ChatSession:
        return ChatSession("local", "Alex", "persona", "", 7, token, "http://srv")

    def test_preset_is_sent_to_the_server(self) -> None:
        app = self.make_app(self.make_session())
        app._handle_style_command("playful")
        args = app.executor.submit.call_args.args
        self.assertEqual(args[1:], ("http://srv", 7, STYLE_PRESETS["playful"], "token"))
        self.assertEqual(app.pending_kind, "style")

    def test_custom_text_and_off_are_supported(self) -> None:
        app = self.make_app(self.make_session())
        app._handle_style_command("dry and sarcastic")
        self.assertEqual(app.executor.submit.call_args.args[3], "dry and sarcastic")
        app.future = None
        app._handle_style_command("off")
        self.assertEqual(app.executor.submit.call_args.args[3], "")

    def test_no_argument_lists_presets_without_a_request(self) -> None:
        app = self.make_app(None)
        app._handle_style_command("")
        app.executor.submit.assert_not_called()
        self.assertIn("casual", app.status)

    def test_requires_an_active_session(self) -> None:
        app = self.make_app(None)
        app._handle_style_command("formal")
        app.executor.submit.assert_not_called()
        self.assertTrue(app.status.startswith("Error"))

    def test_overlong_custom_style_is_rejected_locally(self) -> None:
        app = self.make_app(self.make_session())
        app._handle_style_command("x" * (MAX_STYLE_LENGTH + 1))
        app.executor.submit.assert_not_called()
        self.assertTrue(app.status.startswith("Error"))


if __name__ == "__main__":
    unittest.main()
