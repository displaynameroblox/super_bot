import contextlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import ANY, Mock, patch

from Ai import chat_app, e2ee, transport
from Ai.chat_app import (
    ChatApp,
    ChatSession,
    load_sessions,
    main,
    save_sessions,
    session_event_worker,
    upgrade_legacy_server_url,
)
from Ai.tests.support import URL, make_info


class LegacyServerUrlTests(unittest.TestCase):
    def test_http_addresses_of_saved_chats_are_upgraded(self) -> None:
        self.assertEqual(
            upgrade_legacy_server_url("http://192.168.1.20:8000"),
            "https://192.168.1.20:8000",
        )
        self.assertEqual(upgrade_legacy_server_url("HTTP://host:1"), "https://host:1")
        self.assertEqual(upgrade_legacy_server_url("https://host:1"), "https://host:1")
        self.assertEqual(upgrade_legacy_server_url(""), "")

    def test_saved_sessions_are_upgraded_when_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_sessions.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "local_id": "old",
                            "player_name": "Alex",
                            "persona": "p",
                            "chat_log": "",
                            "server_session_id": 4,
                            "access_token": "t",
                            "server_url": "http://192.168.1.20:8000",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            sessions = load_sessions(path)
            save_sessions(path, sessions)

            self.assertEqual(sessions[0].server_url, "https://192.168.1.20:8000")
            self.assertEqual(load_sessions(path), sessions)
            self.assertIn("https://192.168.1.20:8000", path.read_text(encoding="utf-8"))

    def test_chat_learning_preference_round_trips_and_defaults_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_sessions.json"
            legacy = ChatSession("old", "Alex", "p", "", 4)
            self.assertFalse(legacy.learn_from_chats)
            enabled = ChatSession(
                "new", "Alex", "p", "", 5, learn_from_chats=True
            )
            save_sessions(path, [legacy, enabled])
            self.assertEqual(
                [session.learn_from_chats for session in load_sessions(path)],
                [False, True],
            )


class AppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = transport.TrustStore(Path(directory.name) / "known_servers.json")
        store_patch = patch.object(transport, "_trust_store", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.app = ChatApp.__new__(ChatApp)
        self.app.base_url = "https://10.0.0.1:8000"
        self.app.status = ""
        self.app.sessions = []
        self.app.active_index = -1
        self.app._confirm_lines = Mock(return_value=True)

    def inspect(self, live: transport.ServerInfo):
        return patch.object(transport, "inspect_server", return_value=live)


class ChatLearningRequestTests(unittest.TestCase):
    def test_opt_in_is_included_in_the_encrypted_chat_request_payload(self) -> None:
        with patch.object(chat_app, "request_json", return_value={}) as request:
            chat_app.send_message(
                "https://server.example",
                7,
                "Teach the assistant this preference",
                "token",
                learn_from_chats=True,
            )
        self.assertEqual(request.call_args.args[2]["learn_from_chats"], True)

    def test_learn_command_persists_opt_in_and_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = ChatSession("id", "Alex", "p", "", 7)
            app = ChatApp.__new__(ChatApp)
            app.pending_message = ""
            app.sessions = [session]
            app.active_index = 0
            app.data_file = Path(directory) / "chats.json"
            app.status = ""

            app.handle_command("/learn on")
            self.assertTrue(session.learn_from_chats)
            self.assertIn("SUPER_BOT_COLLECT_CHAT_TRAINING=1", app.status)
            self.assertTrue(load_sessions(app.data_file)[0].learn_from_chats)

            app.handle_command("/learn off")
            self.assertFalse(session.learn_from_chats)
            self.assertFalse(load_sessions(app.data_file)[0].learn_from_chats)

    def test_learn_command_rolls_back_preference_if_save_fails(self) -> None:
        session = ChatSession("id", "Alex", "p", "", 7)
        app = ChatApp.__new__(ChatApp)
        app.pending_message = ""
        app.sessions = [session]
        app.active_index = 0
        app.data_file = Path("unused.json")
        app.status = ""
        with patch.object(chat_app, "save_sessions", side_effect=OSError("disk full")):
            app.handle_command("/learn on")
        self.assertFalse(session.learn_from_chats)
        self.assertIn("disk full", app.status)


class VerifyServerTests(AppTestCase):
    def test_accepting_a_new_server_pins_it_and_connects(self) -> None:
        info = make_info()

        with self.inspect(info):
            self.assertTrue(self.app._verify_server(URL))

        title, lines, prompt, _ = self.app._confirm_lines.call_args.args
        self.assertEqual(title, "VERIFY SERVER")
        self.assertIn(e2ee.format_fingerprint(info.e2ee_fingerprint), "\n".join(lines))
        self.assertIn("[y/N]", prompt)
        self.assertIsNotNone(self.store.get(URL))
        self.assertIn("verified, end-to-end encrypted", self.app.status)

    def test_declining_a_new_server_pins_nothing(self) -> None:
        self.app._confirm_lines.return_value = False

        with self.inspect(make_info()):
            self.assertFalse(self.app._verify_server(URL))

        self.assertIsNone(self.store.get(URL))
        self.assertIn("not trusted", self.app.status)

    def test_a_trusted_server_connects_without_asking(self) -> None:
        info = make_info()
        self.store.pin(URL, info)

        with self.inspect(info):
            self.assertTrue(self.app._verify_server(URL))

        self.app._confirm_lines.assert_not_called()

    def test_a_changed_server_is_refused_and_the_pin_is_kept(self) -> None:
        info = make_info()
        self.store.pin(URL, info)
        impostor = make_info(public_key=e2ee.ServerIdentity.generate().public_key)

        with self.inspect(impostor):
            self.assertFalse(self.app._verify_server(URL))

        title, lines, _, color = self.app._confirm_lines.call_args.args
        self.assertEqual((title, color), ("SERVER IDENTITY CHANGED", 4))
        self.assertIn("/server forget", "\n".join(lines))
        self.assertIn("identity changed", self.app.status)
        self.assertEqual(self.store.get(URL).e2ee_public_key, info.e2ee_public_key)

    def test_unreachable_servers_report_the_error(self) -> None:
        with patch.object(
            transport, "inspect_server", side_effect=transport.TransportError("Could not reach x")
        ):
            self.assertFalse(self.app._verify_server(URL))

        self.assertEqual(self.app.status, "Error: Could not reach x")
        self.app._confirm_lines.assert_not_called()

    def test_a_pin_that_cannot_be_saved_is_reported(self) -> None:
        with (
            self.inspect(make_info()),
            patch.object(transport.TrustStore, "pin", side_effect=OSError("disk full")),
        ):
            self.assertFalse(self.app._verify_server(URL))

        self.assertIn("disk full", self.app.status)


class ConnectToServerTests(AppTestCase):
    def test_a_verified_selection_becomes_the_current_server(self) -> None:
        def select() -> bool:
            self.app.base_url = URL
            return True

        self.app._select_server = Mock(side_effect=select)
        self.app._verify_server = Mock(return_value=True)

        self.assertTrue(self.app._connect_to_server())

        self.app._verify_server.assert_called_once_with(URL)
        self.assertEqual(self.app.base_url, URL)

    def test_a_server_that_fails_verification_does_not_replace_the_current_one(self) -> None:
        def select() -> bool:
            self.app.base_url = URL
            return True

        self.app._select_server = Mock(side_effect=select)
        self.app._verify_server = Mock(return_value=False)

        self.assertFalse(self.app._connect_to_server())

        self.assertEqual(self.app.base_url, "https://10.0.0.1:8000")

    def test_a_cancelled_selection_skips_verification(self) -> None:
        self.app._select_server = Mock(return_value=False)
        self.app._verify_server = Mock()

        self.assertFalse(self.app._connect_to_server())

        self.app._verify_server.assert_not_called()

    def test_connect_and_server_commands_are_dispatched(self) -> None:
        self.app.pending_message = ""
        self.app._connect_to_server = Mock(return_value=True)
        self.app._handle_server_command = Mock()

        self.app.handle_command("/connect")
        self.app.handle_command("/server forget")

        self.app._connect_to_server.assert_called_once_with()
        self.app._handle_server_command.assert_called_once_with("forget")


class ServerCommandTests(AppTestCase):
    def test_without_a_server_there_is_nothing_to_show(self) -> None:
        self.app.base_url = ""

        self.app._handle_server_command("")

        self.assertEqual(self.app.status, "Error: not connected to a server")

    def test_the_current_servers_fingerprints_are_shown(self) -> None:
        info = make_info(self.app.base_url)
        self.store.pin(self.app.base_url, info)

        self.app._handle_server_command("")

        title, lines, _, _ = self.app._confirm_lines.call_args.args
        text = "\n".join(lines)
        self.assertEqual(title, "SERVER")
        self.assertIn(e2ee.format_fingerprint(info.e2ee_fingerprint), text)
        self.assertIn(e2ee.format_fingerprint(info.tls_fingerprint), text)
        self.assertIn("end-to-end encryption", self.app.status)

    def test_an_unverified_server_is_reported(self) -> None:
        self.app._handle_server_command("")

        self.assertIn("not verified yet", self.app.status)
        self.app._confirm_lines.assert_not_called()

    def test_forget_removes_the_pin_so_the_next_connect_asks_again(self) -> None:
        self.store.pin(self.app.base_url, make_info(self.app.base_url))

        self.app._handle_server_command("forget")

        self.assertIsNone(self.store.get(self.app.base_url))
        self.assertIn("Forgot", self.app.status)
        self.app._handle_server_command("forget")
        self.assertIn("nothing to forget", self.app.status)

    def test_the_active_sessions_server_takes_precedence(self) -> None:
        other = "https://10.0.0.9:9000"
        self.store.pin(other, make_info(other))
        self.app.sessions = [ChatSession("l", "Alex", "p", "", 3, "tok", other)]
        self.app.active_index = 0

        self.app._handle_server_command("")

        self.assertIn("10.0.0.9:9000", self.app.status)

    def test_unknown_arguments_are_rejected(self) -> None:
        self.app._handle_server_command("trust")

        self.assertEqual(self.app.status, "Error: use /server or /server forget")

    def test_a_plain_http_server_url_is_reported_not_crashed(self) -> None:
        self.app.base_url = "http://10.0.0.1:8000"

        self.app._handle_server_command("")

        self.assertIn("HTTPS is required", self.app.status)


class ConfirmLinesTests(unittest.TestCase):
    def make_app(self, answer) -> ChatApp:
        app = ChatApp.__new__(ChatApp)
        app.screen = Mock()
        app.screen.getmaxyx.return_value = (24, 60)
        app.screen.get_wch.return_value = answer
        app._put = Mock()
        app._color = Mock(return_value=0)
        return app

    def test_only_y_confirms(self) -> None:
        for answer, expected in (("y", True), ("Y", True), ("n", False), ("\n", False), (121, False)):
            with self.subTest(answer=answer):
                self.assertEqual(
                    self.make_app(answer)._confirm_lines("T", ["line"], "Prompt? ", 3),
                    expected,
                )

    def test_long_fingerprint_lines_are_wrapped_to_the_screen(self) -> None:
        app = self.make_app("y")
        fingerprint = e2ee.format_fingerprint("ab" * 32)

        app._confirm_lines("T", ["Server key:", f"  {fingerprint}"], "Prompt? ", 3)

        drawn = [call.args[2] for call in app._put.call_args_list]
        self.assertTrue(all(len(text) <= 60 - 4 for text in drawn))
        joined = "".join(text.strip() for text in drawn).replace(" ", "")
        self.assertIn(fingerprint.replace(" ", ""), joined)

    def test_the_screen_timeout_is_restored_and_curses_errors_decline(self) -> None:
        app = self.make_app("y")
        app.screen.get_wch.side_effect = chat_app.curses.error("no input")

        self.assertFalse(app._confirm_lines("T", ["x"], "P ", 3))

        self.assertEqual(
            [call.args for call in app.screen.timeout.call_args_list], [(-1,), (100,)]
        )


class FakeStream:
    def __init__(self, items: list, stop: threading.Event) -> None:
        self.items = list(items)
        self.stop = stop

    def recv(self, timeout: float):
        if not self.items:
            self.stop.set()
            return None
        return self.items.pop(0)


def fake_open_stream(*streams_or_errors):
    queue = list(streams_or_errors)

    @contextlib.contextmanager
    def opener(*args, **kwargs):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        yield item

    return opener


class EventWorkerTests(unittest.TestCase):
    def run_worker(self, opener, stop: threading.Event | None = None) -> list[dict]:
        events: Queue = Queue()
        stop = stop or threading.Event()
        with patch.object(chat_app.transport, "open_event_stream", opener):
            session_event_worker(URL, 7, "tok", events, stop)
        return [events.get_nowait() for _ in range(events.qsize())]

    def test_text_only_events_are_forwarded(self) -> None:
        stop = threading.Event()
        stream = FakeStream(
            [
                {"type": "joined", "player_name": "A"},
                {"type": "x", "count": 1},
                None,
                "not an event",
                {"type": "message", "text": "hi"},
            ],
            stop,
        )

        events = self.run_worker(fake_open_stream(stream), stop)

        self.assertEqual(
            events,
            [{"type": "joined", "player_name": "A"}, {"type": "message", "text": "hi"}],
        )

    def test_a_server_that_is_not_trusted_is_reported_once_and_not_retried(self) -> None:
        for error in (
            transport.UntrustedServer("not verified"),
            transport.ServerIdentityChanged("changed"),
            transport.InsecureServerUrl("plain http"),
        ):
            with self.subTest(error=type(error).__name__):
                events = self.run_worker(fake_open_stream(error))
                self.assertEqual(events, [{"type": "error", "text": str(error)}])

    def test_connection_problems_are_reported_once_and_retried(self) -> None:
        stop = threading.Event()
        stop.wait = Mock(return_value=False)
        stream = FakeStream([{"type": "joined", "player_name": "A"}], stop)

        events = self.run_worker(
            fake_open_stream(
                transport.TransportError("Could not reach the server"),
                OSError("reset"),
                stream,
            ),
            stop,
        )

        self.assertEqual(events[0], {"type": "error", "text": "Could not reach the server"})
        self.assertEqual(events[1], {"type": "joined", "player_name": "A"})
        self.assertEqual(len(events), 2)
        self.assertEqual(stop.wait.call_count, 2)


class MainTests(unittest.TestCase):
    def run_main(self, argv: list[str], trusted: bool = True):
        app_class = Mock()
        with (
            patch("sys.argv", ["chat_app", *argv]),
            patch.object(chat_app, "ChatApp", app_class),
            patch.object(chat_app, "DEFAULT_URL", ""),
            patch.object(chat_app.curses, "wrapper", side_effect=lambda function: function(Mock())),
            patch.object(
                chat_app.transport, "confirm_server_on_terminal", return_value=trusted
            ) as confirm,
            patch.object(chat_app.transport, "configure_trust_store") as configure,
            patch("sys.stderr") as stderr,
        ):
            code = main()
        return code, app_class, confirm, configure, stderr

    def test_a_server_address_is_normalised_verified_and_passed_to_the_app(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_file = Path(directory) / "chat_sessions.json"

            code, app_class, confirm, configure, _ = self.run_main(
                ["--url", "192.168.1.20:8000", "--data-file", str(data_file)]
            )

        self.assertEqual(code, 0)
        confirm.assert_called_once_with("https://192.168.1.20:8000")
        configure.assert_called_once_with(data_file.with_name("known_servers.json"))
        app_class.assert_called_once_with(ANY, "https://192.168.1.20:8000", data_file, None)
        app_class.return_value.run.assert_called_once_with()

    def test_plain_http_is_refused_before_anything_starts(self) -> None:
        code, app_class, confirm, _, stderr = self.run_main(["--url", "http://192.168.1.20:8000"])

        self.assertEqual(code, 2)
        app_class.assert_not_called()
        confirm.assert_not_called()
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("HTTPS is required", written)

    def test_an_untrusted_server_exits_without_starting_the_app(self) -> None:
        code, app_class, _, _, _ = self.run_main(
            ["--url", "https://192.168.1.20:8000"], trusted=False
        )

        self.assertEqual(code, 1)
        app_class.assert_not_called()

    def test_a_verification_error_exits_with_a_message(self) -> None:
        app_class = Mock()
        with (
            patch("sys.argv", ["chat_app", "--url", "https://192.168.1.20:8000"]),
            patch.object(chat_app, "ChatApp", app_class),
            patch.object(chat_app.curses, "wrapper") as wrapper,
            patch.object(chat_app.transport, "configure_trust_store"),
            patch.object(
                chat_app.transport,
                "confirm_server_on_terminal",
                side_effect=transport.TransportError("Could not reach 192.168.1.20:8000"),
            ),
            patch("sys.stderr") as stderr,
        ):
            code = main()

        self.assertEqual(code, 1)
        wrapper.assert_not_called()
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("Could not reach", written)

    def test_without_an_address_the_app_discovers_servers_itself(self) -> None:
        code, app_class, confirm, _, _ = self.run_main([])

        self.assertEqual(code, 0)
        confirm.assert_not_called()
        self.assertEqual(app_class.call_args.args[1], "")


class HelpTextTests(unittest.TestCase):
    def test_help_documents_the_server_command_and_https(self) -> None:
        self.assertIn("/server forget", chat_app.HELP_TEXT)
        self.assertIn("end-to-end encryption", chat_app.HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
