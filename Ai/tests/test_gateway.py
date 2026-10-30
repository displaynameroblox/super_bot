import base64
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from Ai import e2ee, gateway, server

REPLY = '{"reply": "Hi!", "is_coding_request": false}'


class GatewayTestCase(unittest.TestCase):
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
        self.phone = phone.start()
        self.addCleanup(phone.stop)
        server.event_manager.connections.clear()
        server.event_manager.participant_ids.clear()
        self.identity = e2ee.ServerIdentity.generate()
        self.now = 1_700_000_000.0
        app = gateway.build_gateway(
            server.app,
            self.identity,
            access_error=server.event_access_error,
            stream_events=server.stream_session_events,
            clock=lambda: self.now,
        )
        # The context manager keeps one event loop, so REST calls and open
        # WebSockets share the same server state.
        client = TestClient(app)
        self.client = client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)

    def seal(self, method: str, path: str, body=None, token: str | None = None):
        return e2ee.seal_request(
            self.identity.public_key,
            {
                "method": method,
                "path": path,
                "authorization": f"Bearer {token}" if token else None,
                "body": body,
            },
            now=self.now,
        )

    def call(self, method: str, path: str, body=None, token: str | None = None):
        sealed, exchange = self.seal(method, path, body, token)
        response = self.client.post("/e2ee", content=sealed)
        self.assertEqual(response.status_code, 200, response.text)
        result = exchange.open_response(response.content)
        return result["status"], result["body"]

    def start_session(self, password: str = "pw") -> dict:
        status, body = self.call(
            "POST",
            "/start_ses",
            {"AI_persona": "a sailor", "plr_name": "Host", "password": password},
        )
        self.assertEqual(status, 200, body)
        return body

    def login(self, session_id: int, password: str = "pw") -> str:
        status, body = self.call(
            "POST", "/login", {"session_ID": session_id, "password": password}
        )
        self.assertEqual(status, 200, body)
        return body["access_token"]

    def hello(self, session_id: int, token: str):
        return e2ee.seal_request(
            self.identity.public_key,
            {"path": f"/events/{session_id}", "authorization": f"Bearer {token}"},
            now=self.now,
        )

    def open_events(self, websocket, session_id: int, token: str):
        sealed, exchange = self.hello(session_id, token)
        websocket.send_bytes(sealed)
        handshake = exchange.open_response(websocket.receive_bytes())
        return handshake, exchange


class PublicSurfaceTests(GatewayTestCase):
    def test_health_and_identity_are_public(self) -> None:
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        identity = self.client.get("/e2ee/identity").json()

        self.assertEqual(identity["version"], e2ee.PROTOCOL_VERSION)
        padded = identity["public_key"] + "=" * (-len(identity["public_key"]) % 4)
        self.assertEqual(base64.urlsafe_b64decode(padded), self.identity.public_key)

    def test_the_plain_api_and_its_documentation_are_not_reachable(self) -> None:
        for method, path in (
            ("POST", "/start_ses"),
            ("POST", "/login"),
            ("POST", "/chat"),
            ("GET", "/chat/1"),
            ("POST", "/style"),
            ("POST", "/skills"),
            ("GET", "/docs"),
            ("GET", "/redoc"),
            ("GET", "/openapi.json"),
        ):
            with self.subTest(path=path):
                response = self.client.request(method, path, json={})
                self.assertEqual(response.status_code, 404)

    def test_plain_websocket_events_are_not_reachable(self) -> None:
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/events/1"):
                pass

    def test_encrypted_responses_are_not_cacheable(self) -> None:
        sealed, _ = self.seal("GET", "/chat/1")

        response = self.client.post("/e2ee", content=sealed)

        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["content-type"], "application/octet-stream")


class EncryptedCallTests(GatewayTestCase):
    def test_the_session_api_works_through_the_envelope(self) -> None:
        grant = self.start_session()
        session_id, host = grant["session_ID"], grant["access_token"]
        guest = self.login(session_id)

        status, reply = self.call(
            "POST",
            "/chat",
            {"ses_id": session_id, "msg": "ahoy", "coding_context": "", "show_progress": True},
            guest,
        )
        status_get, session = self.call("GET", f"/chat/{session_id}", None, host)
        status_style, style = self.call(
            "POST", "/style", {"ses_id": session_id, "style": "terse"}, guest
        )
        status_guest_skills, _ = self.call(
            "POST", "/skills", {"ses_id": session_id, "skills": {"pirate": "Arr."}}, guest
        )
        status_host_skills, skills = self.call(
            "POST", "/skills", {"ses_id": session_id, "skills": {"pirate": "Arr."}}, host
        )

        self.assertEqual(status, 200)
        self.assertIn("ahoy", reply["Chat_log"])
        self.assertEqual((status_get, session["session_ID"]), (200, session_id))
        self.assertEqual((status_style, style), (200, {"AI_style": "terse"}))
        self.assertEqual(status_guest_skills, 403)
        self.assertEqual((status_host_skills, skills), (200, {"AI_skills": {"pirate": "Arr."}}))

    def test_inner_statuses_travel_inside_a_successful_envelope(self) -> None:
        grant = self.start_session()

        statuses = {
            "missing token": self.call("GET", f"/chat/{grant['session_ID']}"),
            "bad password": self.call(
                "POST", "/login", {"session_ID": grant["session_ID"], "password": "no"}
            ),
            "unknown session": self.call("POST", "/login", {"session_ID": 999}),
            "validation": self.call("POST", "/login", {"session_ID": "x"}),
        }

        self.assertEqual(
            {name: result[0] for name, result in statuses.items()},
            {"missing token": 401, "bad password": 401, "unknown session": 404, "validation": 422},
        )

    def test_a_model_failure_is_reported_as_502(self) -> None:
        self.phone.side_effect = server.phone_model.PhoneModelError("phone unreachable")

        status, body = self.call(
            "POST", "/start_ses", {"AI_persona": "p", "plr_name": "n"}
        )

        self.assertEqual(status, 502)
        self.assertIn("phone unreachable", body["detail"])

    def test_unexpected_errors_become_a_sealed_500_and_are_logged(self) -> None:
        with patch.object(server, "generate_reply", side_effect=RuntimeError("boom")):
            with self.assertLogs("uvicorn.error", level="ERROR"):
                status, body = self.call(
                    "POST", "/start_ses", {"AI_persona": "p", "plr_name": "n"}
                )

        self.assertEqual((status, body), (500, {"detail": "Internal Server Error"}))

    def test_only_the_session_api_can_be_called(self) -> None:
        for method, path in (
            ("GET", "/docs"),
            ("GET", "/health"),
            ("GET", "/openapi.json"),
            ("GET", "/events/1"),
            ("GET", "/chat/1/../docs"),
            ("GET", "/chat/12345678901234567890"),
            ("GET", "/chat?x=1"),
            ("GET", "/chat"),
            ("POST", "/chat/1"),
            ("DELETE", "/chat/1"),
            ("POST", "http://evil.example/start_ses"),
            ("POST", "//evil.example/start_ses"),
        ):
            with self.subTest(method=method, path=path):
                status, body = self.call(method, path, {})
                self.assertEqual((status, body), (404, {"detail": "Not Found"}))

    def test_malformed_inner_requests_get_a_sealed_400(self) -> None:
        for payload in (
            "text",
            [],
            {},
            {"method": 1, "path": "/chat"},
            {"method": "GET", "path": None},
            {"method": "GET", "path": "/chat/1", "authorization": 7},
            {"method": "GET", "path": "/chat/1", "authorization": "Bearer é"},
            {"method": "GET", "path": "/chat/1", "authorization": "x" * 600},
        ):
            with self.subTest(payload=payload):
                sealed, exchange = e2ee.seal_request(
                    self.identity.public_key, payload, now=self.now
                )
                response = self.client.post("/e2ee", content=sealed)
                result = exchange.open_response(response.content)
                self.assertEqual(result["status"], 400)


class EnvelopeRejectionTests(GatewayTestCase):
    def post(self, content: bytes, **kwargs):
        return self.client.post("/e2ee", content=content, **kwargs)

    def test_garbage_is_rejected_without_detail(self) -> None:
        for content in (b"", b"hello", b"\x02" * 200):
            with self.subTest(content=content[:8]):
                response = self.post(content)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"error": "invalid_envelope"})

    def test_requests_sealed_to_another_key_are_reported_as_unknown_key(self) -> None:
        other = e2ee.ServerIdentity.generate()
        sealed, _ = e2ee.seal_request(other.public_key, {}, now=self.now)

        response = self.post(sealed)

        self.assertEqual((response.status_code, response.json()), (400, {"error": "unknown_key"}))

    def test_replays_get_409(self) -> None:
        sealed, _ = self.seal("GET", "/chat/1")

        first = self.post(sealed)
        second = self.post(sealed)

        self.assertEqual((first.status_code, second.status_code), (200, 409))
        self.assertEqual(second.json(), {"error": "replayed_request"})

    def test_stale_requests_report_the_server_time(self) -> None:
        sealed, _ = e2ee.seal_request(
            self.identity.public_key, {}, now=self.now - e2ee.MAX_CLOCK_SKEW_SECONDS - 5
        )

        response = self.post(sealed)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(), {"error": "stale_request", "server_time": int(self.now)}
        )

    def test_oversized_bodies_get_413_before_they_are_decrypted(self) -> None:
        response = self.post(b"\x01" * (e2ee.MAX_REQUEST_BYTES + 1))

        self.assertEqual((response.status_code, response.json()), (413, {"error": "too_large"}))

    def test_a_lying_content_length_cannot_bypass_the_limit(self) -> None:
        def chunks():
            for _ in range(3):
                yield b"\x01" * (e2ee.MAX_REQUEST_BYTES // 2)

        response = self.client.post("/e2ee", content=chunks())

        self.assertEqual(response.status_code, 413)

    def test_only_post_is_accepted(self) -> None:
        self.assertEqual(self.client.get("/e2ee").status_code, 405)


class EncryptedEventTests(GatewayTestCase):
    def test_events_are_delivered_encrypted_with_the_right_visibility(self) -> None:
        grant = self.start_session()
        session_id, host = grant["session_ID"], grant["access_token"]
        guest = self.login(session_id)

        with (
            self.client.websocket_connect("/e2ee/events") as host_socket,
            self.client.websocket_connect("/e2ee/events") as guest_socket,
        ):
            handshake, host_exchange = self.open_events(host_socket, session_id, host)
            _, guest_exchange = self.open_events(guest_socket, session_id, guest)
            self.assertEqual(handshake, {"status": 200, "body": {}})
            host_events = host_exchange.stream_decryptor()
            guest_events = guest_exchange.stream_decryptor()

            frame = host_socket.receive_bytes()
            self.assertNotIn(b"joined", frame)
            self.assertEqual(host_events.open(frame)["type"], "joined")
            self.assertEqual(host_events.open(host_socket.receive_bytes())["type"], "joined")
            self.assertEqual(guest_events.open(guest_socket.receive_bytes())["type"], "joined")

            status, _ = self.call(
                "POST",
                "/chat",
                {"ses_id": session_id, "msg": "private", "coding_context": "", "show_progress": False},
                guest,
            )
            self.assertEqual(status, 200)

            def drain(socket, events) -> list[dict]:
                seen = []
                while True:
                    event = events.open(socket.receive_bytes())
                    seen.append(event)
                    if event["type"] == "message":
                        return seen

            host_seen = drain(host_socket, host_events)
            guest_seen = drain(guest_socket, guest_events)

        self.assertEqual(host_seen[0]["phase"], "received")
        self.assertEqual(host_seen[-1]["text"], "private")
        self.assertEqual([event["type"] for event in guest_seen], ["message"])

    def test_a_bad_token_gets_a_sealed_401_and_the_socket_closes(self) -> None:
        grant = self.start_session()

        with self.client.websocket_connect("/e2ee/events") as websocket:
            handshake, _ = self.open_events(websocket, grant["session_ID"], "wrong")
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_bytes()

        self.assertEqual(handshake["status"], 401)
        self.assertEqual(closed.exception.code, 1008)

    def test_unknown_sessions_and_paths_are_refused_with_sealed_errors(self) -> None:
        grant = self.start_session()
        host = grant["access_token"]
        sealed_missing, missing = self.hello(grant["session_ID"] + 5, host)
        sealed_path, wrong_path = e2ee.seal_request(
            self.identity.public_key,
            {"path": "/chat/1", "authorization": f"Bearer {host}"},
            now=self.now,
        )

        results = []
        for sealed, exchange in ((sealed_missing, missing), (sealed_path, wrong_path)):
            with self.client.websocket_connect("/e2ee/events") as websocket:
                websocket.send_bytes(sealed)
                results.append(exchange.open_response(websocket.receive_bytes())["status"])

        self.assertEqual(results, [401, 404])

    def test_invalid_hello_frames_close_the_socket(self) -> None:
        grant = self.start_session()
        sealed, _ = self.hello(grant["session_ID"], grant["access_token"])
        stale, _ = e2ee.seal_request(
            self.identity.public_key,
            {"path": f"/events/{grant['session_ID']}", "authorization": None},
            now=self.now - 10_000,
        )

        for label, send in (
            ("garbage", lambda ws: ws.send_bytes(b"garbage")),
            ("text frame", lambda ws: ws.send_text("hello")),
            ("stale", lambda ws: ws.send_bytes(stale)),
        ):
            with self.subTest(label=label):
                with self.client.websocket_connect("/e2ee/events") as websocket:
                    send(websocket)
                    with self.assertRaises(WebSocketDisconnect) as closed:
                        websocket.receive_bytes()
                    self.assertEqual(closed.exception.code, 1008)
        with self.client.websocket_connect("/e2ee/events") as websocket:
            websocket.send_bytes(sealed)
            websocket.receive_bytes()
        with self.client.websocket_connect("/e2ee/events") as websocket:
            websocket.send_bytes(sealed)
            with self.assertRaises(WebSocketDisconnect):
                websocket.receive_bytes()

    def test_a_silent_client_is_disconnected(self) -> None:
        with patch.object(gateway, "HELLO_TIMEOUT_SECONDS", 0.2):
            with self.client.websocket_connect("/e2ee/events") as websocket:
                with self.assertRaises(WebSocketDisconnect) as closed:
                    websocket.receive_bytes()

        self.assertEqual(closed.exception.code, 1008)

    def test_disconnected_clients_are_removed_from_the_event_manager(self) -> None:
        grant = self.start_session()
        session_id = grant["session_ID"]

        with self.client.websocket_connect("/e2ee/events") as websocket:
            self.open_events(websocket, session_id, grant["access_token"])
            websocket.receive_bytes()
            self.assertEqual(len(server.event_manager.connections[session_id]), 1)

        # Closing the client lets the server loop see the disconnect.
        self.client.get("/health")
        self.assertNotIn(session_id, server.event_manager.connections)


if __name__ == "__main__":
    unittest.main()
