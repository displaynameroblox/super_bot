import asyncio
import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from Ai import e2ee, server, tls

REPLY = '{"reply": "Hi!", "is_coding_request": false}'


class StartServerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.addCleanup(self.reset_server_state)
        self.environment = {
            "SUPER_BOT_SERVER_DIR": str(self.directory / "server"),
            "PORT": "8443",
            "DISCOVERY_PORT": "18766",
            "HOST": "127.0.0.1",
            "SSL_CERTFILE": "",
            "SSL_KEYFILE": "",
            "SSL_KEYFILE_PASSWORD": "",
            "PHONE_MODEL_PROVIDER": "phone",
            "PHONE_MODEL_NAME": "superbot-phone",
            "PHONE_MODEL_BASE_URL": "https://192.168.1.42:8080/v1",
        }

    @staticmethod
    def reset_server_state() -> None:
        server.account_service = server.profiles = server.store = None
        for state in (
            server.chat_sessions,
            server.session_passwords,
            server.session_tokens,
            server.session_token_expiry,
            server.session_hosts,
            server.session_locks,
        ):
            state.clear()

    def start(self, **overrides: str):
        environment = {**self.environment, **overrides}
        output = io.StringIO()
        with (
            patch.dict(os.environ, environment),
            patch.object(server.uvicorn, "run") as run,
            patch.object(server, "socket") as socket_module,
            patch.object(server, "threading") as threading_module,
            contextlib.redirect_stdout(output),
        ):
            server.start_server()
        return run, socket_module, threading_module, output.getvalue()

    def test_the_server_only_serves_tls_and_the_encrypted_gateway(self) -> None:
        run, _, _, _ = self.start()

        run.assert_called_once()
        app = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual((options["host"], options["port"]), ("127.0.0.1", 8443))
        self.assertTrue(Path(options["ssl_certfile"]).is_file())
        self.assertTrue(Path(options["ssl_keyfile"]).is_file())
        self.assertFalse(options["server_header"])
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/e2ee/identity").status_code, 200)
            self.assertEqual(client.post("/start_ses", json={}).status_code, 404)

    def test_the_identity_is_persisted_privately_and_reused(self) -> None:
        first_run, _, _, first_banner = self.start()
        identity_path = self.directory / "server" / server.IDENTITY_FILENAME
        first = e2ee.ServerIdentity.load_or_create(identity_path)
        _, _, _, second_banner = self.start()

        self.assertIn(e2ee.format_fingerprint(first.fingerprint), first_banner)
        self.assertIn(e2ee.format_fingerprint(first.fingerprint), second_banner)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(identity_path.stat().st_mode), 0o600)
        first_identity = TestClient(first_run.call_args.args[0]).get("/e2ee/identity").json()
        self.assertTrue(first_identity["public_key"])

    def test_the_banner_shows_both_fingerprints_and_flags_new_certificates(self) -> None:
        _, _, _, first_banner = self.start()
        _, _, _, second_banner = self.start()
        material = tls.prepare_server_tls(directory=self.directory / "server")

        self.assertIn("HTTPS only", first_banner)
        self.assertIn(e2ee.format_fingerprint(material.fingerprint), first_banner)
        self.assertIn("(self-signed)", first_banner)
        self.assertIn("A new certificate was generated", first_banner)
        self.assertNotIn("A new certificate was generated", second_banner)

    def test_a_configured_certificate_is_used_instead_of_generating_one(self) -> None:
        material = tls.prepare_server_tls(directory=self.directory / "own")

        run, _, _, banner = self.start(
            SSL_CERTFILE=str(material.certfile), SSL_KEYFILE=str(material.keyfile)
        )

        self.assertEqual(run.call_args.kwargs["ssl_certfile"], str(material.certfile))
        self.assertEqual(run.call_args.kwargs["ssl_keyfile"], str(material.keyfile))
        self.assertFalse((self.directory / "server" / tls.CERTIFICATE_FILENAME).exists())
        self.assertNotIn("A new certificate was generated", banner)

    def test_a_key_password_is_passed_to_uvicorn(self) -> None:
        material = tls.prepare_server_tls(directory=self.directory / "own")

        run, _, _, _ = self.start(
            SSL_CERTFILE=str(material.certfile),
            SSL_KEYFILE=str(material.keyfile),
            SSL_KEYFILE_PASSWORD="unused-for-plain-key",
        )

        self.assertEqual(run.call_args.kwargs["ssl_keyfile_password"], "unused-for-plain-key")

    def test_bad_tls_configuration_stops_the_server_before_anything_starts(self) -> None:
        for overrides in (
            {"SSL_CERTFILE": "/nonexistent/cert.pem"},
            {"SSL_KEYFILE": "/nonexistent/key.pem"},
            {"SSL_CERTFILE": "/nonexistent/cert.pem", "SSL_KEYFILE": "/nonexistent/key.pem"},
        ):
            with self.subTest(overrides=overrides):
                with (
                    patch.dict(os.environ, {**self.environment, **overrides}),
                    patch.object(server.uvicorn, "run") as run,
                    patch.object(server, "socket") as socket_module,
                ):
                    with self.assertRaises(ValueError):
                        server.start_server()
                run.assert_not_called()
                socket_module.socket.assert_not_called()

    def test_the_discovery_responder_still_starts(self) -> None:
        _, socket_module, threading_module, _ = self.start()

        socket_module.socket.return_value.bind.assert_called_once_with(("127.0.0.1", 18766))
        threading_module.Thread.return_value.start.assert_called_once_with()

    def test_the_server_is_local_only_unless_lan_access_is_opted_into(self) -> None:
        run, _, _, _ = self.start()
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")

        with (
            patch.dict(os.environ, {**self.environment, "HOST": "0.0.0.0"}),
            patch.object(server.uvicorn, "run") as blocked_run,
        ):
            with self.assertRaisesRegex(ValueError, "SUPER_BOT_ALLOW_LAN"):
                server.start_server()
        blocked_run.assert_not_called()

        run, socket_module, _, banner = self.start(HOST="0.0.0.0", SUPER_BOT_ALLOW_LAN="1")
        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
        socket_module.socket.return_value.bind.assert_called_once_with(("0.0.0.0", 18766))
        self.assertIn("LAN access is enabled", banner)

    def test_a_busy_port_stops_startup_with_a_clear_message(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, self.environment),
            patch.object(server.uvicorn, "run") as run,
            patch.object(server, "socket") as socket_module,
            contextlib.redirect_stdout(output),
        ):
            socket_module.create_server.side_effect = OSError(98, "Address already in use")
            with self.assertRaisesRegex(RuntimeError, "already be using this port"):
                server.start_server()
        run.assert_not_called()

    def test_sessions_are_durable_across_a_server_restart(self) -> None:
        self.start()
        self.assertIsNotNone(server.store)
        server.chat_sessions.clear()
        server.chat_sessions[41] = server.Chatsession(
            Chat_log="hello", AI_persona="p", plr_name="Alex", session_ID=41
        )
        server.persist_session(41)
        token = server.issue_access_token(41)
        server.chat_sessions.clear()
        server.session_tokens.clear()
        self.start()  # a fresh start reloads everything from SQLite
        self.assertEqual(server.chat_sessions[41].Chat_log, "hello")
        self.assertEqual(server.authorized_session_id(f"Bearer {token}"), 41)

    def test_a_plain_http_phone_link_is_called_out(self) -> None:
        _, _, _, banner = self.start(PHONE_MODEL_BASE_URL="http://192.168.1.42:8080/v1")
        _, _, _, safe_banner = self.start()

        self.assertIn("Warning: the link to the phone model", banner)
        self.assertNotIn("Warning:", safe_banner)

    def test_server_banner_names_selected_model_provider(self) -> None:
        _, _, _, banner = self.start()
        self.assertIn("AI provider: Phone-hosted model (superbot-phone)", banner)

    def test_gemini_provider_is_named_in_server_banner(self) -> None:
        with patch.dict(
            os.environ,
            {
                **self.environment,
                "PHONE_MODEL_PROVIDER": "gemini",
                "GEMINI_API_KEY": "test-key",
            },
        ):
            _, _, _, banner = self.start(
                PHONE_MODEL_PROVIDER="gemini",
                GEMINI_API_KEY="test-key",
            )
        self.assertIn("Google Gemini cloud model", banner)
        self.assertIn("messages are sent to Google", banner)
        self.assertNotIn("Warning:", banner)

    def test_invalid_ports_are_still_rejected(self) -> None:
        with (
            patch.dict(os.environ, {**self.environment, "PORT": "abc"}),
            patch.object(server.uvicorn, "run") as run,
        ):
            with self.assertRaises(ValueError):
                server.start_server()
        run.assert_not_called()
        self.assertFalse((self.directory / "server").exists())


class PlainEventRouteTests(unittest.TestCase):
    """The inner app keeps its plain routes; they are only reachable in-process."""

    def setUp(self) -> None:
        patcher = patch.multiple(
            server,
            chat_sessions={},
            session_tokens={},
            session_hosts={},
            session_passwords={},
            session_locks={},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        phone = patch("Ai.server.phone_model.chat", return_value=REPLY)
        phone.start()
        self.addCleanup(phone.stop)
        server.event_manager.connections.clear()
        server.event_manager.participant_ids.clear()

    def test_access_errors_distinguish_missing_tokens_from_missing_sessions(self) -> None:
        with TestClient(server.app) as client:
            grant = client.post(
                "/start_ses", json={"AI_persona": "p", "plr_name": "n"}
            ).json()
        header = f"Bearer {grant['access_token']}"

        self.assertIsNone(server.event_access_error(grant["session_ID"], header))
        self.assertEqual(
            server.event_access_error(grant["session_ID"], None),
            (401, "Valid session login required"),
        )
        self.assertEqual(
            server.event_access_error(grant["session_ID"] + 1, header),
            (401, "Valid session login required"),
        )
        server.chat_sessions.clear()
        self.assertEqual(
            server.event_access_error(grant["session_ID"], header),
            (404, "Chat session not found"),
        )

    def test_a_client_that_drops_during_the_handshake_is_not_registered(self) -> None:
        with TestClient(server.app) as client:
            grant = client.post(
                "/start_ses", json={"AI_persona": "p", "plr_name": "Alex"}
            ).json()

        class DroppedSocket:
            async def accept(self) -> None:
                raise WebSocketDisconnect(1006)

        asyncio.run(
            server.stream_session_events(
                DroppedSocket(), grant["session_ID"], f"Bearer {grant['access_token']}"
            )
        )

        self.assertEqual(server.event_manager.connections, {})
        self.assertEqual(server.event_manager.participant_ids, {})

    def test_the_header_authenticated_route_still_streams_events(self) -> None:
        with TestClient(server.app) as client:
            grant = client.post(
                "/start_ses", json={"AI_persona": "p", "plr_name": "Alex"}
            ).json()
            headers = {"Authorization": f"Bearer {grant['access_token']}"}
            with client.websocket_connect(
                f"/events/{grant['session_ID']}", headers=headers
            ) as websocket:
                self.assertEqual(
                    websocket.receive_json(), {"type": "joined", "player_name": "Alex"}
                )
            with self.assertRaises(WebSocketDisconnect) as refused:
                with client.websocket_connect(f"/events/{grant['session_ID']}"):
                    pass

        self.assertEqual(refused.exception.code, 1008)
        self.assertEqual(refused.exception.reason, "Valid session login required")


if __name__ == "__main__":
    unittest.main()
