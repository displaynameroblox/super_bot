import asyncio
import hashlib
import json
import os
import sqlite3
import socket
import threading
import tempfile
import unittest
from itertools import count
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from Ai import server
from Ai.chat_app import discover_servers


class FakeWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, str]] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, event: dict[str, str]) -> None:
        self.events.append(event)


class SessionServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        server.chat_sessions.clear()
        server.session_passwords.clear()
        server.session_tokens.clear()
        server.session_hosts.clear()
        server.session_locks.clear()
        server.pending_session_ids.clear()
        server.event_manager.connections.clear()
        server.event_manager.participant_ids.clear()
        server.session_ids = count(1)

    async def _create_session(
        self,
        session_id: int | None = None,
        password: str = "",
    ) -> server.SessionGrant:
        request = server.start_ses_req(
            Chat_log="",
            AI_persona="Helpful assistant",
            plr_name="Alex",
            session_ID=session_id,
            password=password,
        )
        with (
            patch.object(
                server,
                "generate_reply",
                return_value=server.AssistantResponse(
                    reply="Hello!",
                    is_coding_request=False,
                ),
            ),
        ):
            return await server.start_ses(request)

    async def test_password_session_requires_login_token_for_reads_and_messages(self) -> None:
        grant = await self._create_session(session_id=47, password="correct horse")

        with self.assertRaises(HTTPException) as missing_token:
            server.get_chat(47, authorization=None)
        self.assertEqual(missing_token.exception.status_code, 401)

        with self.assertRaises(HTTPException) as incorrect_password:
            server.login(
                server.LoginRequest(session_ID=47, password="incorrect"),
            )
        self.assertEqual(incorrect_password.exception.status_code, 401)

        login_grant = server.login(
            server.LoginRequest(session_ID=47, password="correct horse"),
        )
        self.assertNotEqual(login_grant.access_token, grant.access_token)
        authorized = server.get_chat(
            47,
            authorization=f"Bearer {login_grant.access_token}",
        )
        self.assertEqual(authorized.session_ID, 47)

        with self.assertRaises(HTTPException) as wrong_session:
            server.get_chat(
                48,
                authorization=f"Bearer {login_grant.access_token}",
            )
        self.assertEqual(wrong_session.exception.status_code, 401)

    async def test_passwordless_generated_session_broadcasts_shared_chat_updates(self) -> None:
        grant = await self._create_session()
        self.assertGreater(grant.session_ID, 0)
        self.assertIsNone(server.session_passwords[grant.session_ID])
        self.assertEqual(
            server.session_hosts[grant.session_ID],
            hashlib.sha256(grant.access_token.encode("ascii")).digest(),
        )

        login_grant = server.login(server.LoginRequest(session_ID=grant.session_ID))
        websocket = FakeWebSocket()
        server.event_manager.connections[grant.session_ID] = {websocket}  # type: ignore[assignment]

        with (
            patch.object(
                server,
                "generate_reply",
                return_value=server.AssistantResponse(
                    reply="I can help.",
                    is_coding_request=True,
                ),
            ) as generate_reply,
        ):
            response = await server.chat(
                server.Chatreg(
                    ses_id=grant.session_ID,
                    msg="Please help",
                    coding_context="Use the Roblox skill.",
                ),
                authorization=f"Bearer {login_grant.access_token}",
            )

        self.assertIn("Please help", response.Chat_log)
        self.assertIn("I can help.", response.Chat_log)
        self.assertTrue(response.is_coding_request)
        generate_reply.assert_called_once()
        self.assertEqual(generate_reply.call_args.args[2], "Use the Roblox skill.")
        self.assertEqual(generate_reply.call_args.args[3], "Please help")
        self.assertEqual(websocket.events[-1]["type"], "message")
        self.assertEqual(websocket.events[-1]["chat_log"], response.Chat_log)

    async def test_chat_training_requires_both_client_and_server_opt_in(self) -> None:
        grant = await self._create_session()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "chat-training.sqlite"
            with (
                patch.dict(
                    os.environ,
                    {
                        "SUPER_BOT_COLLECT_CHAT_TRAINING": "1",
                        "SUPER_BOT_CHAT_TRAINING_DB": str(database),
                    },
                ),
                patch.object(
                    server,
                    "generate_reply",
                    return_value=server.AssistantResponse(
                        reply="I will keep answers brief.",
                        is_coding_request=False,
                    ),
                ),
            ):
                await server.chat(
                    server.Chatreg(
                        ses_id=grant.session_ID,
                        msg="Please keep answers brief.",
                        learn_from_chats=True,
                    ),
                    authorization=f"Bearer {grant.access_token}",
                )
                await server.chat(
                    server.Chatreg(
                        ses_id=grant.session_ID,
                        msg="This message is not opted in.",
                        learn_from_chats=False,
                    ),
                    authorization=f"Bearer {grant.access_token}",
                )

            self.assertFalse(database.exists(), "The server must never retain training exchanges")

    async def test_progress_is_visible_to_creator_and_opted_in_sender_only(self) -> None:
        creator = await self._create_session()
        sender = server.login(server.LoginRequest(session_ID=creator.session_ID))
        creator_socket = FakeWebSocket()
        sender_socket = FakeWebSocket()
        await server.event_manager.connect(
            creator.session_ID,
            server.access_token_id(f"Bearer {creator.access_token}") or b"",
            creator_socket,  # type: ignore[arg-type]
        )
        await server.event_manager.connect(
            creator.session_ID,
            server.access_token_id(f"Bearer {sender.access_token}") or b"",
            sender_socket,  # type: ignore[arg-type]
        )

        with (
            patch.object(
                server,
                "generate_reply",
                return_value=server.AssistantResponse(
                    reply="Here is the answer.",
                    is_coding_request=False,
                ),
            ),
        ):
            await server.chat(
                server.Chatreg(
                    ses_id=creator.session_ID,
                    msg="Help me",
                    show_progress=False,
                ),
                authorization=f"Bearer {sender.access_token}",
            )
            await asyncio.sleep(0)

        creator_progress = [
            event for event in creator_socket.events if event["type"] == "progress"
        ]
        sender_progress = [
            event for event in sender_socket.events if event["type"] == "progress"
        ]
        self.assertTrue(creator_progress)
        self.assertEqual(sender_progress, [])
        self.assertIn("drafting_reply", [event["phase"] for event in creator_progress])

    async def test_phone_model_generates_reply_and_classification_in_one_call(self) -> None:
        assistant_response = server.AssistantResponse(
            reply="I'll work out a plan.",
            is_coding_request=True,
        )
        phone_chat = Mock(
            return_value=assistant_response.model_dump_json()
        )
        session = server.Chatsession(
            Chat_log="Earlier context",
            AI_persona="Helpful",
            plr_name="Alex",
            session_ID=4,
        )

        with patch.object(server.phone_model, "chat", phone_chat):
            result = server.generate_reply(
            session,
            "[2026-01-01T12:00:00+00:00] Alex: Hi\n"
            "[2026-01-01T12:00:01+00:00] AI: Hello, Alex.",
            "Selected skills: python",
            "Build me a feature",
            )

        self.assertEqual(result, assistant_response)
        phone_chat.assert_called_once()
        messages = phone_chat.call_args.args[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Selected skills: python", messages[0]["content"])
        self.assertEqual(
            [message["role"] for message in messages[1:]],
            ["user", "assistant", "user"],
        )
        self.assertEqual(messages[1]["content"], "Hi")
        self.assertEqual(messages[2]["content"], "Hello, Alex.")
        self.assertEqual(
            messages[-1],
            {"role": "user", "content": "Build me a feature"},
        )
        self.assertEqual(
            phone_chat.call_args.kwargs["response_schema"],
            server.AssistantResponse.model_json_schema(),
        )

    async def test_initial_greeting_is_followed_by_a_user_turn_in_model_history(
        self,
    ) -> None:
        messages = server._conversation_messages(
            "[2026-01-01T12:00:00+00:00] AI: Hello, Alex.",
            "How are you?",
        )

        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "(Start a friendly conversation.)"},
                {"role": "assistant", "content": "Hello, Alex."},
                {"role": "user", "content": "How are you?"},
            ],
        )

    async def test_phone_model_receives_structured_reply_schema(self) -> None:
        assistant_response = server.AssistantResponse(
            reply="I will keep you updated.",
            is_coding_request=False,
        )
        session = server.Chatsession(
            Chat_log="",
            AI_persona="Helpful",
            plr_name="Alex",
            session_ID=4,
        )

        with patch.object(
            server.phone_model,
            "chat",
            return_value=assistant_response.model_dump_json(),
        ):
            result = server.generate_reply(
                session,
                "",
            )

        self.assertEqual(result, assistant_response)

    async def test_custom_session_id_collision_is_rejected(self) -> None:
        await self._create_session(session_id=84)
        request = server.start_ses_req(
            Chat_log="",
            AI_persona="Helpful assistant",
            plr_name="Alex",
            session_ID=84,
        )
        with self.assertRaises(HTTPException) as collision:
            await server.start_ses(request)
        self.assertEqual(collision.exception.status_code, 409)

    async def test_client_discovers_only_app_advertisements(self) -> None:
        discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        discovery_socket.bind(("127.0.0.1", 0))
        discovery_socket.settimeout(2)
        discovery_port = discovery_socket.getsockname()[1]

        def advertise_once() -> None:
            try:
                payload, address = discovery_socket.recvfrom(256)
                if payload == server.DISCOVERY_REQUEST:
                    discovery_socket.sendto(b"SUPER_BOT_SERVER_V1:8123", address)
            finally:
                discovery_socket.close()

        responder = threading.Thread(target=advertise_once, daemon=True)
        responder.start()
        with patch.dict(os.environ, {"DISCOVERY_PORT": str(discovery_port)}):
            discovered = discover_servers(timeout=0.5)
        responder.join(timeout=2)
        self.assertIn("https://127.0.0.1:8123", discovered)


if __name__ == "__main__":
    unittest.main()
