import os
import socket
import threading
import unittest
from itertools import count
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from fastapi import HTTPException
from google.genai import Client

from Ai import server
from Ai.chat_app import discover_servers


class FakeWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, str]] = []

    async def send_json(self, event: dict[str, str]) -> None:
        self.events.append(event)


class SessionServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        server.chat_sessions.clear()
        server.session_passwords.clear()
        server.session_tokens.clear()
        server.session_locks.clear()
        server.pending_session_ids.clear()
        server.event_manager.connections.clear()
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
            patch.object(server, "get_client", return_value=object()),
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
        self.assertEqual(grant.session_ID, 1)
        self.assertIsNone(server.session_passwords[1])

        login_grant = server.login(server.LoginRequest(session_ID=1))
        websocket = FakeWebSocket()
        server.event_manager.connections[1] = {websocket}  # type: ignore[assignment]

        with (
            patch.object(server, "get_client", return_value=object()),
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
                    ses_id=1,
                    msg="Please help",
                    coding_context="Use the Roblox skill.",
                ),
                authorization=f"Bearer {login_grant.access_token}",
            )

        self.assertIn("Please help", response.Chat_log)
        self.assertIn("I can help.", response.Chat_log)
        self.assertTrue(response.is_coding_request)
        generate_reply.assert_called_once()
        self.assertEqual(generate_reply.call_args.args[3], "Use the Roblox skill.")
        self.assertEqual(generate_reply.call_args.args[4], "Please help")
        self.assertEqual(websocket.events[-1]["type"], "message")
        self.assertEqual(websocket.events[-1]["chat_log"], response.Chat_log)

    async def test_gemini_generates_reply_and_classification_in_one_structured_call(self) -> None:
        assistant_response = server.AssistantResponse(
            reply="I'll work out a plan.",
            is_coding_request=True,
        )
        generate_content = Mock(
            return_value=type(
                "Response",
                (),
                {"parsed": assistant_response, "text": assistant_response.model_dump_json()},
            )()
        )
        client = cast(
            Client,
            SimpleNamespace(
                models=SimpleNamespace(generate_content=generate_content)
            ),
        )
        session = server.Chatsession(
            Chat_log="Earlier context",
            AI_persona="Helpful",
            plr_name="Alex",
            session_ID=4,
        )

        result = server.generate_reply(
            client,
            session,
            "Earlier context\nAlex: Build me a feature",
            "Selected skills: python",
            "Build me a feature",
        )

        self.assertEqual(result, assistant_response)
        generate_content.assert_called_once()
        config = generate_content.call_args.kwargs["config"]
        self.assertEqual(config["response_mime_type"], "application/json")
        self.assertIs(config["response_schema"], server.AssistantResponse)
        prompt = generate_content.call_args.kwargs["contents"]
        self.assertIn("Build me a feature", prompt)
        self.assertIn("Selected skills: python", prompt)

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
        self.assertIn("http://127.0.0.1:8123", discovered)


if __name__ == "__main__":
    unittest.main()
