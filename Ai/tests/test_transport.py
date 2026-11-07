import json
import os
import ssl
import stat
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from Ai import e2ee, tls, transport
from Ai.tests.support import URL, make_info


class UrlPolicyTests(unittest.TestCase):
    def test_https_addresses_are_parsed(self) -> None:
        self.assertEqual(transport.parse_server_url(URL).key, "192.168.1.20:8000")
        self.assertEqual(transport.parse_server_url("https://Example.COM").key, "example.com:443")
        self.assertEqual(transport.parse_server_url("https://[::1]:9000/").key, "[::1]:9000")

    def test_plain_http_is_refused(self) -> None:
        for url in ("http://192.168.1.20:8000", "HTTP://localhost", "ws://x", "192.168.1.20:8000", ""):
            with self.subTest(url=url):
                with self.assertRaises(transport.InsecureServerUrl):
                    transport.parse_server_url(url)

    def test_malformed_addresses_are_refused(self) -> None:
        for url in (
            "https://user:pw@example.com",
            "https://example.com/path",
            "https://example.com?x=1",
            "https://example.com#frag",
            "https://",
            "https://example.com:99999",
        ):
            with self.subTest(url=url):
                with self.assertRaises(transport.TransportError):
                    transport.parse_server_url(url)


class TrustStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "known_servers.json"
        self.store = transport.TrustStore(self.path)

    def test_an_unknown_server_has_no_pin(self) -> None:
        self.assertIsNone(self.store.get(URL))
        self.assertFalse(self.store.forget(URL))

    def test_pins_round_trip_and_are_private(self) -> None:
        info = make_info()

        pinned = self.store.pin(URL, info)
        loaded = self.store.get(URL)

        self.assertEqual(loaded, pinned)
        self.assertEqual(loaded.e2ee_public_key, info.e2ee_public_key)
        self.assertEqual(loaded.tls_certificate, info.tls_certificate)
        self.assertEqual(loaded.e2ee_fingerprint, info.e2ee_fingerprint)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_a_ca_verified_server_stores_no_certificate(self) -> None:
        self.store.pin(URL, make_info(ca_verified=True))

        loaded = self.store.get(URL)

        self.assertIsNone(loaded.tls_certificate)
        self.assertIsNone(loaded.tls_fingerprint)

    def test_servers_are_keyed_by_host_and_port_case_insensitively(self) -> None:
        self.store.pin("https://Example.com:8000", make_info("https://example.com:8000"))

        self.assertIsNotNone(self.store.get("https://example.COM:8000"))
        self.assertIsNone(self.store.get("https://example.com:8001"))
        self.assertIsNone(self.store.get("https://example.com"))

    def test_pinning_again_replaces_and_forget_removes_only_that_server(self) -> None:
        first, second = make_info(), make_info()
        other_url = "https://10.0.0.5:8000"
        self.store.pin(URL, first)
        self.store.pin(other_url, make_info(other_url))
        self.store.pin(URL, second)

        self.assertEqual(self.store.get(URL).e2ee_public_key, second.e2ee_public_key)
        self.assertTrue(self.store.forget(URL))
        self.assertIsNone(self.store.get(URL))
        self.assertIsNotNone(self.store.get(other_url))

    def test_a_corrupt_store_is_reported_instead_of_silently_reset(self) -> None:
        self.path.parent.mkdir(parents=True)
        for content in ("{not json", "[]", json.dumps({"servers": []})):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(transport.TransportError):
                    self.store.get(URL)
                with self.assertRaises(transport.TransportError):
                    self.store.pin(URL, make_info())
        self.assertEqual(self.path.read_text(), json.dumps({"servers": []}))

    def test_a_damaged_entry_is_reported(self) -> None:
        self.path.parent.mkdir(parents=True)
        valid_key = transport._b64encode(bytes(32))
        for record in (
            {"e2ee_public_key": "AAAA"},
            {"e2ee_public_key": 5},
            {"tls_certificate": "x"},
            {"e2ee_public_key": valid_key, "tls_certificate": "not a certificate"},
            {"e2ee_public_key": valid_key, "tls_certificate": 7},
            "text",
        ):
            with self.subTest(record=record):
                self.path.write_text(json.dumps({"servers": {"192.168.1.20:8000": record}}))
                with self.assertRaises(transport.TransportError):
                    self.store.get(URL)

    def test_the_default_store_lives_in_the_app_data_directory(self) -> None:
        with patch.object(transport, "_trust_store", None):
            self.assertEqual(
                transport.trust_store().path, tls.app_data_dir() / "known_servers.json"
            )
            configured = transport.configure_trust_store(self.path)
            self.assertIs(transport.trust_store(), configured)


class CheckServerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = transport.TrustStore(Path(directory.name) / "known_servers.json")
        self.info = make_info()

    def check(self, live: transport.ServerInfo) -> transport.ServerCheck:
        with patch.object(transport, "inspect_server", return_value=live):
            return transport.check_server(URL, store=self.store)

    def test_unknown_server_is_new(self) -> None:
        self.assertEqual(self.check(self.info).state, "new")

    def test_matching_server_is_trusted(self) -> None:
        self.store.pin(URL, self.info)

        check = self.check(self.info)

        self.assertEqual(check.state, "trusted")
        self.assertIsNotNone(check.pinned)

    def test_a_new_encryption_key_is_a_change(self) -> None:
        self.store.pin(URL, self.info)
        live = make_info(public_key=e2ee.ServerIdentity.generate().public_key)

        check = self.check(live)

        self.assertEqual(check.state, "changed")
        self.assertIn("encryption key", check.reason)

    def test_a_new_certificate_is_a_change(self) -> None:
        self.store.pin(URL, self.info)
        live = make_info(public_key=self.info.e2ee_public_key, certificate_index=1)

        check = self.check(live)

        self.assertEqual(check.state, "changed")
        self.assertIn("certificate", check.reason)

    def test_a_ca_pinned_server_that_turns_self_signed_is_a_change(self) -> None:
        self.store.pin(URL, make_info(public_key=self.info.e2ee_public_key, ca_verified=True))

        check = self.check(make_info(public_key=self.info.e2ee_public_key))

        self.assertEqual(check.state, "changed")
        self.assertIn("authority", check.reason)


class TerminalConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = transport.TrustStore(Path(directory.name) / "known_servers.json")
        self.info = make_info()
        self.said: list[str] = []

    def confirm(self, answer: str | Exception, live: transport.ServerInfo | None = None) -> tuple[bool, list[str]]:
        asked: list[str] = []

        def ask(prompt: str) -> str:
            asked.append(prompt)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch.object(transport, "inspect_server", return_value=live or self.info):
            result = transport.confirm_server_on_terminal(
                URL, ask=ask, say=self.said.append, store=self.store
            )
        return result, asked

    def test_accepting_a_new_server_pins_it(self) -> None:
        result, asked = self.confirm(" Y ")

        self.assertTrue(result)
        self.assertEqual(len(asked), 1)
        self.assertIsNotNone(self.store.get(URL))
        shown = "\n".join(self.said)
        self.assertIn(e2ee.format_fingerprint(self.info.e2ee_fingerprint), shown)
        self.assertIn(e2ee.format_fingerprint(self.info.tls_fingerprint), shown)

    def test_declining_or_a_closed_stdin_does_not_pin(self) -> None:
        for answer in ("", "n", "yes please", EOFError()):
            with self.subTest(answer=answer):
                result, _ = self.confirm(answer)
                self.assertFalse(result)
                self.assertIsNone(self.store.get(URL))

    def test_a_trusted_server_needs_no_prompt(self) -> None:
        self.store.pin(URL, self.info)

        result, asked = self.confirm("n")

        self.assertTrue(result)
        self.assertEqual(asked, [])

    def test_a_changed_server_is_refused_with_a_warning_and_never_pinned(self) -> None:
        self.store.pin(URL, self.info)
        impostor = make_info(public_key=e2ee.ServerIdentity.generate().public_key)

        result, asked = self.confirm("y", live=impostor)

        self.assertFalse(result)
        self.assertEqual(asked, [])
        self.assertIn("WARNING", self.said[0])
        self.assertEqual(self.store.get(URL).e2ee_public_key, self.info.e2ee_public_key)


class DescriptionTests(unittest.TestCase):
    def test_self_signed_servers_show_both_fingerprints(self) -> None:
        info = make_info()

        text = "\n".join(transport.describe_server(info))

        self.assertIn("192.168.1.20:8000", text)
        self.assertIn(e2ee.format_fingerprint(info.e2ee_fingerprint), text)
        self.assertIn(e2ee.format_fingerprint(info.tls_fingerprint), text)

    def test_ca_verified_servers_only_show_the_encryption_key(self) -> None:
        info = make_info(ca_verified=True)

        text = "\n".join(transport.describe_server(info))

        self.assertIn(e2ee.format_fingerprint(info.e2ee_fingerprint), text)
        self.assertNotIn(e2ee.format_fingerprint(info.tls_fingerprint), text)
        self.assertIn("certificate authority", text)


class RequestErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = transport.TrustStore(Path(directory.name) / "known_servers.json")
        self.identity = e2ee.ServerIdentity.generate()
        info = make_info(public_key=self.identity.public_key, ca_verified=True)
        self.store.pin(URL, info)

    def request(self):
        return transport.secure_request(URL, "GET", "/chat/1", timeout=5, store=self.store)

    def http_error(self, status: int, body: dict) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            URL, status, "error", {}, BytesIO(json.dumps(body).encode())
        )

    def test_unknown_servers_are_refused_before_any_connection(self) -> None:
        with patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(transport.UntrustedServer):
                transport.secure_request(
                    "https://10.9.9.9:8000", "GET", "/chat/1", timeout=5, store=self.store
                )
        urlopen.assert_not_called()

    def test_envelope_rejections_get_specific_messages(self) -> None:
        cases = {
            "unknown_key": (transport.ServerIdentityChanged, "no longer matches"),
            "stale_request": (transport.TransportError, "clock"),
            "too_large": (transport.TransportError, "too large"),
            "replayed_request": (transport.TransportError, "repeated"),
            "invalid_envelope": (transport.TransportError, "could not process"),
        }
        for code, (error_type, text) in cases.items():
            with self.subTest(code=code):
                body = {"error": code, "server_time": 10}
                with patch("urllib.request.urlopen", side_effect=self.http_error(400, body)):
                    with self.assertRaises(error_type) as caught:
                        self.request()
                self.assertIn(text, str(caught.exception))

    def test_an_unreadable_error_body_still_gives_a_clear_error(self) -> None:
        broken = urllib.error.HTTPError(URL, 502, "bad gateway", {}, BytesIO(b"<html>"))
        with patch("urllib.request.urlopen", side_effect=broken):
            with self.assertRaises(transport.TransportError) as caught:
                self.request()
        self.assertIn("HTTP 502", str(caught.exception))

    def test_certificate_failures_mean_the_identity_changed(self) -> None:
        reason = ssl.SSLCertVerificationError("certificate verify failed")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(reason)):
            with self.assertRaises(transport.ServerIdentityChanged):
                self.request()

    def test_network_failures_are_reported_as_unreachable(self) -> None:
        for error in (
            urllib.error.URLError(ConnectionRefusedError("refused")),
            TimeoutError("timed out"),
            ConnectionResetError("reset"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch("urllib.request.urlopen", side_effect=error):
                    with self.assertRaises(transport.TransportError) as caught:
                        self.request()
                self.assertIn("Could not reach the server", str(caught.exception))

    def test_a_response_from_the_wrong_key_means_the_identity_changed(self) -> None:
        impostor = e2ee.ServerIdentity.generate()

        def impostor_response(request, **kwargs):
            exchange = e2ee.open_request(
                impostor,
                e2ee.seal_request(impostor.public_key, {})[0],
                replay_cache=e2ee.ReplayCache(),
            )
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = exchange.seal_response({"status": 200, "body": {}})
            return response

        with patch("urllib.request.urlopen", side_effect=impostor_response):
            with self.assertRaises(transport.ServerIdentityChanged):
                self.request()

    def test_a_malformed_inner_response_is_rejected(self) -> None:
        def respond(request, **kwargs):
            exchange = e2ee.open_request(
                self.identity,
                request.data,
                replay_cache=e2ee.ReplayCache(),
            )
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = exchange.seal_response({"body": {}})
            return response

        with patch("urllib.request.urlopen", side_effect=respond):
            with self.assertRaises(transport.TransportError):
                self.request()

    def test_the_token_and_path_are_inside_the_sealed_body(self) -> None:
        seen = {}

        def respond(request, **kwargs):
            seen["headers"] = dict(request.header_items())
            seen["url"] = request.full_url
            seen["data"] = request.data
            exchange = e2ee.open_request(
                self.identity, request.data, replay_cache=e2ee.ReplayCache()
            )
            seen["payload"] = exchange.payload
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = exchange.seal_response({"status": 200, "body": {"ok": 1}})
            return response

        with patch("urllib.request.urlopen", side_effect=respond):
            status, body = transport.secure_request(
                URL, "POST", "/chat", {"msg": "hi"}, "tok-123", timeout=5, store=self.store
            )

        self.assertEqual((status, body), (200, {"ok": 1}))
        self.assertEqual(seen["url"], f"{URL}/e2ee")
        self.assertNotIn("Authorization", seen["headers"])
        self.assertNotIn(b"tok-123", seen["data"])
        self.assertNotIn(b"/chat", seen["data"])
        self.assertEqual(
            seen["payload"],
            {
                "method": "POST",
                "path": "/chat",
                "authorization": "Bearer tok-123",
                "body": {"msg": "hi"},
            },
        )


class ContextTests(unittest.TestCase):
    def test_an_unreadable_extra_ca_file_is_reported(self) -> None:
        with patch.dict(os.environ, {"SUPER_BOT_CA_CERT": "/nonexistent/ca.pem"}):
            with self.assertRaises(transport.TransportError):
                transport.system_context()

    def test_system_context_verifies_certificates_and_hostnames(self) -> None:
        with patch.dict(os.environ, {"SUPER_BOT_CA_CERT": ""}):
            context = transport.system_context()

        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_pinned_context_trusts_only_the_pinned_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            material = tls.prepare_server_tls(directory=Path(directory))
            context = transport.pinned_context(material.certfile.read_text())

        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.cert_store_stats()["x509"], 1)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)


class EventHeartbeatTests(unittest.TestCase):
    def test_slow_events_keep_the_connection_open(self):
        websocket = MagicMock()
        websocket.recv.side_effect = [b"handshake", TimeoutError(), b"event"]
        exchange = MagicMock()
        exchange.open_response.return_value = {"status": 200}
        exchange.stream_decryptor.return_value.open.return_value = {"type": "progress"}
        with (
            patch.object(transport, "_require_pin") as pin,
            patch.object(transport, "_context_for"),
            patch.object(transport.e2ee, "seal_request", return_value=(b"hello", exchange)),
            patch("websockets.sync.client.connect") as connect,
        ):
            pin.return_value.e2ee_public_key = b"key"
            connect.return_value.__enter__.return_value = websocket
            with transport.open_event_stream(URL, 7, "token") as stream:
                self.assertIsNone(stream.recv(timeout=0.01))
                self.assertEqual(stream.recv(timeout=0.01), {"type": "progress"})
            self.assertIsNone(connect.call_args.kwargs["ping_timeout"])
            self.assertEqual(connect.call_count, 1)


if __name__ == "__main__":
    unittest.main()
