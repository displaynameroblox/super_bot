import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from Ai import gateway, server, transport, web_app, web_client

def remote_backend(http: TestClient):
    """Stands in for the encrypted transport: only gateway-allowed routes reach the server."""

    async def backend(method, path, body, bearer):
        if not any(method == m and p.fullmatch(path) for m, p in gateway.ALLOWED_ROUTES):
            return 404, {"detail": "Not Found"}
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        response = http.request(method, path, json=body if method != "GET" else None, headers=headers)
        return response.status_code, response.json()

    return backend


PORT = 18001
HOST = f"127.0.0.1:{PORT}"
CSRF = {"X-Super-Bot-Web": "1"}


class WebClientTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.multiple(server, account_service=None, profiles=None, store=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        for state in (server.chat_sessions, server.session_tokens, server.session_token_expiry):
            self.addCleanup(state.clear)
        server.init_persistence(Path(directory.name) / "data" / "super_bot.db")
        self.backend = remote_backend(TestClient(server.app))
        self.client = TestClient(
            web_app.build_web_app(self.backend, PORT),
            base_url=f"http://{HOST}",
            headers=CSRF,
        )

    def register(self, name: str = "alice", client: TestClient | None = None):
        client = client or self.client
        return client.post("/api/register", json={"username": name, "password": f"password-{name}"})

    def test_static_files_are_served_with_strict_security_headers(self) -> None:
        for path, marker in (("/", "<title>Super Bot</title>"), ("/app.js", "api("), ("/style.css", "--accent")):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, response.text)
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.client.get("/../server.py").status_code, 404)
        self.assertEqual(self.client.get("/%2e%2e/server.py").status_code, 404)

    def test_the_page_loads_nothing_external_and_holds_no_secrets(self) -> None:
        text = "".join((web_app.WEB_DIRECTORY / name).read_text() for name in ("index.html", "app.js"))
        for forbidden in ("http://", "https://", "localStorage", "sessionStorage", "innerHTML", "api_key", "GEMINI"):
            self.assertNotIn(forbidden, text)

    def test_register_sets_an_httponly_cookie_and_never_returns_the_token(self) -> None:
        response = self.register()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access_token", response.json())
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        me = self.client.get("/api/me").json()
        self.assertEqual(me["username"], "alice")

    def test_requests_without_a_session_are_refused(self) -> None:
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(self.client.post("/api/chats", json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/send", json={"session_id": 1, "message": "hi"}).status_code, 401)

    def test_dns_rebinding_and_cross_site_requests_are_refused(self) -> None:
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example:18001"}).status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host": "127.0.0.1:9999"}).status_code, 403)
        body = {"username": "mallory", "password": "password-mallory"}
        self.assertEqual(
            self.client.post("/api/register", json=body, headers={"Origin": "http://evil.example"}).status_code, 403
        )
        bare = TestClient(web_app.build_web_app(self.backend, PORT), base_url=f"http://{HOST}")
        self.assertEqual(bare.post("/api/register", json=body).status_code, 403)
        self.assertEqual(server.account_service.user_count(), 0)
        self.assertEqual(
            self.client.post("/api/register", json=body, headers={"Origin": f"http://{HOST}"}).status_code, 200
        )

    def test_bad_and_oversized_bodies_are_rejected(self) -> None:
        self.assertEqual(self.client.post("/api/login", content=b"not json").status_code, 400)
        self.assertEqual(self.client.post("/api/login", content=b"[1]").status_code, 400)
        self.assertEqual(self.client.post("/api/login", content=b"{" + b" " * 70000 + b"}").status_code, 400)

    def test_logout_ends_the_session_and_clears_the_cookie(self) -> None:
        self.register()
        self.assertEqual(self.client.post("/api/logout").status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_a_stolen_cookie_stops_working_after_logout_everywhere(self) -> None:
        self.register()
        stolen = self.client.cookies.get(web_app.COOKIE_NAME)
        self.assertEqual(self.client.post("/api/logout_all").status_code, 200)
        other = TestClient(
            web_app.build_web_app(self.backend, PORT), base_url=f"http://{HOST}", headers=CSRF,
            cookies={web_app.COOKIE_NAME: stolen},
        )
        self.assertEqual(other.get("/api/me").status_code, 401)

    def test_login_errors_do_not_reveal_which_part_was_wrong(self) -> None:
        self.register()
        self.client.cookies.clear()
        bad = self.client.post("/api/login", json={"username": "alice", "password": "wrong-password"})
        self.assertEqual(bad.status_code, 401)
        self.assertNotIn("set-cookie", bad.headers)

    def test_chatting_works_and_is_private_to_the_owner(self) -> None:
        reply = server.AssistantResponse(reply="Hello from the bot", is_coding_request=False)
        with patch.object(server, "generate_reply", return_value=reply):
            self.register("alice")
            created = self.client.post("/api/sessions", json={})
            self.assertEqual(created.status_code, 200, created.text)
            self.assertNotIn("access_token", created.json())
            session_id = created.json()["session_ID"]

            sent = self.client.post("/api/send", json={"session_id": session_id, "message": "hi bot"})
            self.assertEqual(sent.status_code, 200, sent.text)
            self.assertIn("hi bot", sent.json()["Chat_log"])
            self.assertIn("Hello from the bot", sent.json()["Chat_log"])

            listed = self.client.post("/api/chats", json={"query": "hi bot"}).json()["chats"]
            self.assertEqual([c["session_id"] for c in listed], [session_id])

            bob = TestClient(web_app.build_web_app(self.backend, PORT), base_url=f"http://{HOST}", headers=CSRF)
            self.register("bob", bob)
            self.assertEqual(bob.post("/api/chats", json={}).json()["chats"], [])
            self.assertEqual(bob.post("/api/chats/get", json={"session_id": session_id}).status_code, 404)
            self.assertEqual(bob.post("/api/send", json={"session_id": session_id, "message": "x"}).status_code, 404)
            self.assertEqual(bob.post("/api/chats/delete", json={"session_id": session_id}).status_code, 404)

            self.assertEqual(self.client.post("/api/chats/delete", json={"session_id": session_id}).status_code, 200)
            self.assertEqual(
                self.client.post("/api/send", json={"session_id": session_id, "message": "again"}).status_code, 404
            )

    def test_send_validates_input(self) -> None:
        self.register()
        for body in ({}, {"session_id": "1", "message": "x"}, {"session_id": 1, "message": "  "}, {"session_id": True, "message": "x"}):
            self.assertEqual(self.client.post("/api/send", json=body).status_code, 400, body)

    def test_unknown_api_routes_and_methods_are_not_found(self) -> None:
        self.register()
        self.assertEqual(self.client.post("/api/start_ses", json={}).status_code, 404)
        self.assertEqual(self.client.post("/api/nope", json={}).status_code, 404)
        self.assertEqual(self.client.get("/api/chats").status_code, 404)

    def test_profile_rejects_secret_like_settings(self) -> None:
        self.register()
        response = self.client.post("/api/profile", json={"model_settings": {"api_key": "x"}})
        self.assertEqual(response.status_code, 400)


class ClientSideTests(unittest.TestCase):
    def test_unreachable_server_becomes_a_502_not_a_crash(self) -> None:
        def broken(*args, **kwargs):
            raise transport.TransportError("Could not reach the server: refused")

        backend = web_client.make_backend("https://127.0.0.1:1", broken)
        client = TestClient(web_app.build_web_app(backend, PORT), base_url=f"http://{HOST}", headers=CSRF)
        response = client.post("/api/login", json={"username": "a", "password": "b"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("Could not reach the server", response.json()["detail"])

    def test_the_backend_uses_the_pinned_transport_with_the_bearer_token(self) -> None:
        calls = []

        def fake(url, method, path, body, token, *, timeout):
            calls.append((url, method, path, body, token))
            return 200, {"ok": True}

        import asyncio
        backend = web_client.make_backend("https://server.example:8000", fake)
        result = asyncio.run(backend("POST", "/accounts/chats", {"query": ""}, "tok"))
        self.assertEqual(result, (200, {"ok": True}))
        self.assertEqual(calls, [("https://server.example:8000", "POST", "/accounts/chats", {"query": ""}, "tok")])

    def test_the_server_no_longer_hosts_a_web_client(self) -> None:
        self.assertFalse(hasattr(server, "start_web_client"))


if __name__ == "__main__":
    unittest.main()
