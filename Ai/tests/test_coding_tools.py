import json
import curses
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

from Ai import coding_tools, transport
from Ai.protocol import CHAT_SKILLS, STYLE_PRESETS
from Ai.chat_app import (
    ChatApp,
    ChatProfile,
    ChatSession,
    MAX_INITIAL_CHAT_LOG_LENGTH,
    ProfileStore,
    ProgressUpdate,
    ServerError,
    UIPreferences,
    load_ui_preferences,
    normalize_server_url,
    request_json,
    load_profiles,
    load_sessions,
    save_profiles,
    save_sessions,
    save_ui_preferences,
)


class CodingToolsTests(unittest.TestCase):
    def test_startup_login_prompt_authenticates_before_chat(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.closed = False
        app.base_url = "http://localhost"
        app.executor = Mock()
        app.executor.submit.return_value = Future()
        app._prompt = Mock(side_effect=["23", "secret"])
        app._wait_for_startup_request = Mock(
            side_effect=lambda: (
                setattr(
                    app,
                    "sessions",
                    [
                        ChatSession(
                            local_id="joined",
                            player_name="Alex",
                            persona="Friendly",
                            chat_log="",
                            server_session_id=23,
                            access_token="token",
                        )
                    ],
                ),
                setattr(app, "active_index", 0),
            )
        )

        self.assertTrue(app._startup_login())
        self.assertEqual(app._prompt.call_args_list[0].kwargs, {"title": "LOGIN"})
        self.assertEqual(
            app._prompt.call_args_list[1].kwargs,
            {"secret": True, "title": "LOGIN"},
        )
        app.executor.submit.assert_called_once_with(
            ANY,
            "http://localhost",
            23,
            "secret",
        )
        app._wait_for_startup_request.assert_called_once_with()

    def test_run_does_not_draw_chat_when_startup_login_is_cancelled(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.base_url = "http://localhost"
        app.closed = False
        app._connect_to_server = Mock(return_value=True)
        app._startup_login = Mock(return_value=False)
        app.draw = Mock()
        app.session_event_stop = threading.Event()
        app.agent_process = None
        app.executor = Mock()

        app.run()

        app.draw.assert_not_called()
        app.executor.shutdown.assert_called_once_with(
            wait=False,
            cancel_futures=True,
        )

    def test_server_address_normalization_requires_https(self) -> None:
        self.assertEqual(
            normalize_server_url("192.168.1.20:8000"),
            "https://192.168.1.20:8000",
        )
        self.assertEqual(
            normalize_server_url("https://example.com:9000/"),
            "https://example.com:9000",
        )
        self.assertEqual(normalize_server_url("  "), "")
        for address in (
            "http://user:password@example.com",
            "https://user:password@example.com",
            "https://example.com/path",
        ):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    normalize_server_url(address)
        with self.assertRaisesRegex(ValueError, "HTTPS is required"):
            normalize_server_url("http://192.168.1.20:8000")

    def test_request_json_sends_the_token_inside_the_encrypted_call(self) -> None:
        with patch(
            "Ai.chat_app.transport.secure_request",
            return_value=(200, {"ok": True}),
        ) as secure_request:
            result = request_json(
                "https://192.168.1.20:8000",
                "/chat",
                {"msg": "hello"},
                access_token="opaque-token",
            )
            request_json("https://192.168.1.20:8000", "/chat/7", access_token="t")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            secure_request.call_args_list[0],
            call(
                "https://192.168.1.20:8000",
                "POST",
                "/chat",
                {"msg": "hello"},
                "opaque-token",
                timeout=ANY,
            ),
        )
        self.assertEqual(secure_request.call_args_list[1].args[1], "GET")

    def test_request_json_maps_server_and_transport_errors(self) -> None:
        with patch(
            "Ai.chat_app.transport.secure_request",
            return_value=(404, {"detail": "Chat session not found"}),
        ):
            with self.assertRaises(ServerError) as missing:
                request_json("https://192.168.1.20:8000", "/chat/9", access_token="t")
        self.assertEqual(missing.exception.status_code, 404)
        self.assertIn("Chat session not found", str(missing.exception))

        with patch(
            "Ai.chat_app.transport.secure_request",
            side_effect=transport.UntrustedServer("The server has not been verified yet"),
        ):
            with self.assertRaises(ServerError) as untrusted:
                request_json("https://192.168.1.20:8000", "/chat/9")
        self.assertIsNone(untrusted.exception.status_code)
        self.assertIn("not been verified", str(untrusted.exception))

        with patch(
            "Ai.chat_app.transport.secure_request",
            return_value=(200, ["not", "an", "object"]),
        ):
            with self.assertRaises(ServerError):
                request_json("https://192.168.1.20:8000", "/chat/9")

    def test_ui_preferences_round_trip_and_legacy_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ui_preferences.json"
            self.assertEqual(load_ui_preferences(path), UIPreferences())
            preferences = UIPreferences(
                show_progress=False,
                show_navigation_help=False,
            )
            save_ui_preferences(path, preferences)
            self.assertEqual(load_ui_preferences(path), preferences)

    def test_keyboard_navigation_cycles_tabs_and_f_keys_jump_to_tab(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.active_tab = "Chat"
        app.view_mode = "chat"
        app.scroll_offset = 0
        app.coding_scroll_offset = 0
        app.action_menu_visible = False

        app.handle_key("\t")
        self.assertEqual(app.active_tab, "Activity")
        app.handle_key(curses.KEY_LEFT)
        self.assertEqual(app.active_tab, "Chat")
        app.handle_key(curses.KEY_F0 + 4)
        self.assertEqual(app.active_tab, "Coding")
        app.handle_key(curses.KEY_F0 + 5)
        self.assertEqual(app.active_tab, "Review")

    def test_view_command_selects_each_tab_and_split(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.active_tab = "Chat"
        app.view_mode = "chat"
        app.scroll_offset = 0
        app.coding_scroll_offset = 0
        app.action_menu_visible = False
        app.pending_message = ""

        for view, expected_tab in (
            ("activity", "Activity"),
            ("coding", "Coding"),
            ("review", "Review"),
            ("chat", "Chat"),
        ):
            app.handle_command(f"/view {view}")
            self.assertEqual(app.active_tab, expected_tab)
            self.assertEqual(app.status, f"Changed view to {view}")
        app.handle_command("/view split")
        self.assertEqual(app.active_tab, "Chat")
        self.assertEqual(app.view_mode, "split")

    def test_activity_progress_expires_and_hides_own_updates(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.sessions = [
            ChatSession(
                local_id="own",
                player_name="Host",
                persona="Helpful",
                chat_log="",
                server_session_id=23,
                access_token="own-token",
            )
        ]
        app.active_index = 0
        app.ui_preferences = UIPreferences(show_progress=False)
        app.session_events = Queue()
        own_id = app._active_participant_id()
        app.progress_updates = {
            own_id: ProgressUpdate(own_id, "Host", "thinking", "Private", 99),
            "guest": ProgressUpdate("guest", "Guest", "thinking", "Working", 99),
            "stale": ProgressUpdate("stale", "Old guest", "thinking", "", 30),
            "finished": ProgressUpdate("finished", "Done", "complete", "", 94),
        }

        with patch("Ai.chat_app.time.monotonic", return_value=100):
            app._poll_session_events()
            lines = app._activity_panel_lines()

        rendered = "\n".join(text for line in lines for text, _ in line)
        self.assertIn("Guest", rendered)
        self.assertNotIn("Private", rendered)
        self.assertNotIn("Host · client", rendered)
        self.assertNotIn("stale", app.progress_updates)
        self.assertNotIn("finished", app.progress_updates)

    def test_patch_review_shortcuts_do_not_consume_message_text(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.active_tab = "Review"
        app.input_text = ""
        app.future = None
        app.action_menu_visible = False
        app.help_visible = False
        app._apply_agent_patch = Mock()
        app._discard_agent_patch = Mock()

        app.handle_key("a")
        self.assertEqual(app.input_text, "a")
        app.handle_key("\x01")
        app.handle_key("\x04")

        app._apply_agent_patch.assert_called_once_with()
        app._discard_agent_patch.assert_called_once_with()

    def test_f1_toggles_and_saves_navigation_hints(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.nav_help_visible = True
        app.ui_preferences = UIPreferences()
        app.ui_preferences_path = Path("unused")
        app._save_ui_preferences = Mock()

        app.handle_key(curses.KEY_F0 + 1)

        self.assertFalse(app.nav_help_visible)
        self.assertFalse(app.ui_preferences.show_navigation_help)
        app._save_ui_preferences.assert_called_once_with()

    def test_legacy_saved_session_loads_without_auth_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "chat_sessions.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "local_id": "legacy",
                            "player_name": "Alex",
                            "persona": "Friendly assistant",
                            "chat_log": "Hello",
                            "server_session_id": 23,
                        }
                    ]
                ),
                encoding="utf-8",
            )

            sessions = load_sessions(path)
            self.assertEqual(sessions[0].access_token, "")
            self.assertEqual(sessions[0].server_url, "")
            save_sessions(path, sessions)
            self.assertEqual(load_sessions(path), sessions)

    def test_coding_state_round_trip_and_invalid_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "coding_state.json"
            state = coding_tools.CodingState(
                provider="custom",
                command_template="custom-cli --prompt {prompt}",
                coding_mode="auto",
                workspace="/projects/example",
                skills=["python", "roblox-luau"],
                custom_skills={"team-style": "Use focused tests."},
                plan="A reviewed plan",
                job_status="completed",
                job_patch="diff --git a/file b/file",
            )
            coding_tools.save_coding_state(path, state)
            self.assertEqual(coding_tools.load_coding_state(path), state)
            path.write_text(json.dumps({"skills": ["python", 123]}), encoding="utf-8")
            self.assertEqual(coding_tools.load_coding_state(path).skills, [])

    def test_build_agent_argv_keeps_prompt_as_one_argument(self) -> None:
        prompt = "Implement this; do not split into tokens"
        argv = coding_tools.build_agent_argv("copilot -p {prompt}", prompt)
        self.assertEqual(argv, ["copilot", "-p", prompt])
        with self.assertRaises(ValueError):
            coding_tools.build_agent_argv("copilot -p", prompt)

    def test_skills_include_builtins_and_discovered_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            skill_path = workspace / ".github" / "skills" / "testing" / "SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("Run focused tests.", encoding="utf-8")
            skills = coding_tools.discover_skills(workspace)
            self.assertIn("python", skills)
            self.assertIn("lua", skills)
            self.assertIn("roblox-luau", skills)
            self.assertEqual(skills["testing"], "Run focused tests.")

    def test_agent_uses_sandbox_and_returns_reviewable_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "project"
            workspace.mkdir()
            tracked_file = workspace / "tracked.txt"
            tracked_file.write_text("original\n", encoding="utf-8")
            secret_file = workspace / ".env"
            secret_file.write_text("API_KEY=not-for-agent\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet"], cwd=workspace, check=True)
            subprocess.run(["git", "add", "--all"], cwd=workspace, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                cwd=workspace,
                check=True,
            )

            script = (
                "from pathlib import Path; "
                "assert not Path('.env').exists(); "
                "Path('tracked.txt').write_text('agent edit\\n'); "
                "Path('new.txt').write_text('new file\\n'); "
                "print('agent completed')"
            )
            template = f"{shlex.quote(sys.executable)} -c {{prompt}}"
            process = coding_tools.AgentProcess(template, script, workspace)
            process.reader.join(timeout=30)
            self.assertFalse(process.reader.is_alive(), "agent process timed out")
            self.assertEqual(process.returncode, 0)
            output = process.collect_output()
            self.assertIn("agent completed", output)
            self.assertIn("tracked.txt", process.patch)
            self.assertIn("new.txt", process.patch)
            self.assertEqual(tracked_file.read_text(encoding="utf-8"), "original\n")
            self.assertEqual(secret_file.read_text(encoding="utf-8"), "API_KEY=not-for-agent\n")

            tracked_file.write_text("newer user change\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                coding_tools.apply_agent_patch(workspace, process.patch)
            self.assertEqual(
                tracked_file.read_text(encoding="utf-8"),
                "newer user change\n",
            )
            tracked_file.write_text("original\n", encoding="utf-8")
            coding_tools.apply_agent_patch(workspace, process.patch)
            self.assertEqual(tracked_file.read_text(encoding="utf-8"), "agent edit\n")
            self.assertEqual((workspace / "new.txt").read_text(encoding="utf-8"), "new file\n")

    def test_phone_model_planning_uses_brief_instruction_and_selected_skills(self) -> None:
        phone_chat = Mock(return_value="1. Change the feature\n2. Test it")
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(coding_tools.phone_model, "chat", phone_chat):
                plan = coding_tools.make_plan(
                    Path(temp_dir),
                    "Add a health check",
                    [
                        {"role": "user", "content": "Recent coding-mode request"},
                        {"role": "assistant", "content": "Existing coding brief"},
                    ],
                    "## python\nUse type hints.",
                    chat_context="The user means the health-check project discussed earlier.",
                )
        self.assertIn("Test it", plan)
        prompt = phone_chat.call_args.args[0][1]["content"]
        self.assertIn("Recent coding-mode request", prompt)
        self.assertIn("health-check project discussed earlier", prompt)
        self.assertIn("Recent normal-chat context:", prompt)
        self.assertIn(
            "implementation brief",
            phone_chat.call_args.args[0][0]["content"],
        )

    def test_coding_history_round_trips_and_rejects_malformed_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "coding_state.json"
            state = coding_tools.CodingState(
                coding_history=[
                    {"role": "user", "content": "Implement a feature"},
                    {"role": "agent", "content": "Changed one file"},
                ]
            )
            coding_tools.save_coding_state(path, state)
            self.assertEqual(
                coding_tools.load_coding_state(path).coding_history,
                state.coding_history,
            )
            path.write_text(
                json.dumps({"coding_history": [{"role": [], "content": "bad"}]}),
                encoding="utf-8",
            )
            self.assertEqual(
                coding_tools.load_coding_state(path).coding_history,
                [],
            )
            path.write_text(json.dumps({"coding_mode": ["auto"]}), encoding="utf-8")
            self.assertEqual(
                coding_tools.load_coding_state(path).coding_mode,
                "brief",
            )

    def test_phone_model_coding_detection_accepts_only_yes_or_no(self) -> None:
        with patch.object(
            coding_tools,
            "ask_phone_model",
            return_value=" YES\n",
        ) as ask:
            self.assertTrue(
                coding_tools.detect_coding_request(
                    Path("/workspace"),
                    "Let's make a feature",
                    "## python\nUse tests.",
                )
            )
        self.assertIn("software", ask.call_args.args[0])
        self.assertIn("Recent normal-chat context:", ask.call_args.args[1])
        with patch.object(coding_tools, "ask_phone_model", return_value="maybe"):
            with self.assertRaises(ValueError):
                coding_tools.detect_coding_request(
                    Path("/workspace"),
                    "Hello",
                    "No skills.",
                )

    def test_coding_history_limits_context_and_excludes_normal_chat(self) -> None:
        phone_chat = Mock(return_value="Implement the described feature.")
        with patch.object(coding_tools.phone_model, "chat", phone_chat):
            coding_tools.make_plan(
                Path("/workspace"),
                "Add feature",
                [{"role": "user", "content": "Coding discussion only"}],
                "## python\nUse tests.",
            )
        prompt = phone_chat.call_args.args[0][1]["content"]
        self.assertIn("Coding discussion only", prompt)
        self.assertNotIn("normal chat transcript", prompt)

    def test_phone_model_command_suggestion_uses_constrained_json_and_is_parsed(self) -> None:
        phone_chat = Mock(return_value='{"command":"git status --short"}')
        with patch.object(coding_tools.phone_model, "chat", phone_chat):
            command = coding_tools.suggest_command(
                Path("/workspace"),
                "Show changed files",
            )

        self.assertEqual(command, "git status --short")
        self.assertEqual(
            phone_chat.call_args.kwargs["response_schema"],
            {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    def test_custom_skills_are_included_in_shared_skill_catalog(self) -> None:
        custom = {"my-style": "Prefer small functions and focused tests."}
        with tempfile.TemporaryDirectory() as temp_dir:
            skills = coding_tools.discover_skills(Path(temp_dir), custom)
        self.assertEqual(skills["my-style"], custom["my-style"])
        self.assertIn(
            "my-style",
            coding_tools.selected_skill_text(["my-style"], skills),
        )

    def test_user_can_create_edit_and_delete_custom_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = ChatApp.__new__(ChatApp)
            app.workspace = Path(temp_dir)
            app.skills = coding_tools.discover_skills(app.workspace)
            app.coding_state = coding_tools.CodingState()
            app._save_coding_state = Mock()

            app._handle_skill_command("create team-style Prefer focused tests")
            self.assertEqual(
                app.coding_state.custom_skills["team-style"],
                "Prefer focused tests",
            )
            app._handle_skill_command("edit team-style Add type checking")
            self.assertEqual(
                app.skills["team-style"],
                "Add type checking",
            )
            app.coding_state.skills.append("team-style")
            app._handle_skill_command("delete team-style")
            self.assertNotIn("team-style", app.coding_state.custom_skills)
            self.assertNotIn("team-style", app.coding_state.skills)
            app._save_coding_state.assert_called()

    def test_chat_message_is_sent_directly_to_server_with_coding_context(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.input_text = "Fix that issue we discussed"
        app.retry_due = None
        app.pending_message = ""
        app.coding_only_mode = False
        app.coding_future = None
        app.coding_state = coding_tools.CodingState(
            coding_history=[{"role": "user", "content": "Earlier code task"}]
        )
        app.ui_preferences = UIPreferences()
        app.skills = {}
        app.sessions = [
            ChatSession(
                local_id="chat",
                player_name="Alex",
                persona="Friendly",
                chat_log="The earlier project discussion.",
                server_session_id=23,
                access_token="session-token",
                server_url="http://localhost",
            )
        ]
        app.active_index = 0
        app.executor = Mock()
        app.executor.submit.return_value = Future()
        app.status = ""
        app.coding_future_kind = ""
        app.base_url = "http://localhost"

        app.submit_input()

        submitted = app.executor.submit.call_args
        self.assertEqual(submitted.args[0], ANY)
        self.assertEqual(submitted.args[1:5], (
            "http://localhost",
            23,
            "Fix that issue we discussed",
            "session-token",
        ))
        self.assertIn("Earlier code task", submitted.args[5])
        self.assertTrue(submitted.args[6])
        self.assertIn("The earlier project discussion.", app.pending_message_chat_context)

    def test_server_classification_starts_coding_after_chat_reply(self) -> None:
        app = ChatApp.__new__(ChatApp)
        with tempfile.TemporaryDirectory() as temp_dir:
            app.data_file = Path(temp_dir) / "sessions.json"
            app.sessions = [
                ChatSession(
                    local_id="chat",
                    player_name="Alex",
                    persona="Friendly",
                    chat_log="Old history",
                    server_session_id=17,
                    access_token="session-token",
                )
            ]
            app.active_index = 0
            app.future = Future()
            app.future.set_result(
                {
                    "Chat_log": "Old history\nAlex: Make this feature\nAI: I'll plan it.",
                    "is_coding_request": True,
                }
            )
            app.pending_kind = "message"
            app.pending_message = "Make this feature"
            app.pending_message_session_id = 17
            app.pending_message_chat_context = "Old history"
            app.pending_message_coding_context = "selected skills"
            app.pending_message_can_start_coding = True
            app.status = ""
            app.scroll_offset = 0
            app.coding_state = coding_tools.CodingState()
            app.ui_preferences = UIPreferences()
            app.coding_state.coding_history = []
            app._confirm_coding_mode = Mock(return_value=True)
            app._start_plan = Mock()
            app._save_coding_state = Mock()

            app._finish_pending_request()

        app._start_plan.assert_called_once_with("Make this feature")
        self.assertEqual(app.coding_request_chat_context, "Old history")
        self.assertIn("I'll plan it.", app.sessions[0].chat_log)

    def test_server_non_coding_classification_does_not_start_plan(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.future = Future()
        app.future.set_result(
            {
                "Chat_log": "Alex: How are you?\nAI: Doing well.",
                "is_coding_request": False,
            }
        )
        app.pending_kind = "message"
        app.sessions = [
            ChatSession(
                local_id="chat",
                player_name="Alex",
                persona="Friendly",
                chat_log="",
                server_session_id=17,
                access_token="session-token",
            )
        ]
        app.active_index = 0
        app.pending_message = "How are you?"
        app.pending_message_session_id = 17
        app.pending_message_chat_context = ""
        app.pending_message_coding_context = ""
        app.pending_message_can_start_coding = True
        app.ui_preferences = UIPreferences()
        app.status = ""
        app.scroll_offset = 0
        app.coding_state = coding_tools.CodingState()
        app._confirm_coding_mode = Mock()
        app._start_plan = Mock()
        app.data_file = Path("/tmp/super-bot-test-sessions.json")
        with patch("Ai.chat_app.save_sessions"):
            app._finish_pending_request()

        app._confirm_coding_mode.assert_not_called()
        app._start_plan.assert_not_called()

    def test_gemini_message_failure_offers_code_only_agent_fallback(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.future = Future()
        app.future.set_exception(ServerError("Phone model is unavailable", 502))
        app.pending_kind = "message"
        app.pending_message = "Fix the build"
        app.pending_message_session_id = 17
        app.pending_message_chat_context = "Earlier coding discussion"
        app.pending_message_coding_context = ""
        app.pending_message_can_start_coding = True
        app.retry_due = None
        app.retry_delay_seconds = 5
        app.coding_only_mode = False
        app.code_only_queue = []
        app.coding_state = coding_tools.CodingState(
            command_template="copilot -p {prompt}",
        )
        app.coding_state.coding_history = []
        app.skills = {}
        app.workspace = Path("/workspace")
        app.coding_future = None
        app.agent_process = None
        app.executor = Mock()
        app.executor.submit.return_value = Future()
        app.view_mode = "chat"
        app._confirm_code_only_mode = Mock(return_value=True)
        app._save_coding_state = Mock()
        app.status = ""
        app.scroll_offset = 0
        app.sessions = []
        app.data_file = Path("/tmp/super-bot-test-sessions.json")

        with patch("Ai.chat_app.save_sessions"):
            app._finish_pending_request()

        self.assertTrue(app.coding_only_mode)
        self.assertEqual(app.pending_message, "")
        self.assertEqual(app.coding_future_kind, "agent_start")
        app.executor.submit.assert_called_once()
        self.assertIs(app.executor.submit.call_args.args[0], coding_tools.AgentProcess)
        self.assertIn("Fix the build", app.executor.submit.call_args.args[2])

    def test_gemini_rate_limit_offers_code_only_fallback(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.future = Future()
        app.future.set_exception(ServerError("Phone model is rate limited", 429))
        app.pending_kind = "message"
        app.pending_message = "Fix the build"
        app.pending_message_session_id = 17
        app.pending_message_chat_context = ""
        app.pending_message_coding_context = ""
        app.pending_message_can_start_coding = True
        app._enable_code_only_mode = Mock(return_value=True)
        app.status = ""

        app._finish_pending_request()

        app._enable_code_only_mode.assert_called_once()
        self.assertEqual(
            app._enable_code_only_mode.call_args.args[1],
            "Fix the build",
        )
        self.assertEqual(app.pending_message, "")

    def test_declining_code_only_fallback_keeps_message_available_for_retry(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.future = Future()
        app.future.set_exception(ServerError("Phone model is unavailable", 503))
        app.pending_kind = "message"
        app.pending_message = "Please fix this"
        app.pending_message_session_id = 17
        app.pending_message_chat_context = ""
        app.pending_message_coding_context = ""
        app.pending_message_can_start_coding = False
        app.retry_due = None
        app.retry_delay_seconds = 5
        app._confirm_code_only_mode = Mock(return_value=False)
        app._confirm_message_retry = Mock(return_value=True)
        app.status = ""

        with patch("Ai.chat_app.time.monotonic", return_value=100):
            app._finish_pending_request()

        self.assertEqual(app.pending_message, "Please fix this")
        self.assertEqual(app.pending_message_session_id, 17)
        self.assertEqual(app.retry_due, 105)

    def test_failed_initial_gemini_greeting_can_continue_without_chat_session(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.future = Future()
        app.future.set_exception(ServerError("Phone model could not start", 503))
        app.pending_kind = "new"
        app.pending_session_data = ("Alex", "Helpful")
        app.coding_only_mode = False
        app.sessions = []
        app.active_index = -1
        app._confirm_code_only_mode = Mock(return_value=True)
        app.status = ""

        app._finish_pending_request()

        self.assertTrue(app.coding_only_mode)
        self.assertIsNone(app.active_session)
        self.assertIn("Code-only mode enabled", app.status)

    def test_code_only_followup_waits_for_running_agent_and_patch_review(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.coding_only_mode = True
        app.code_only_queue = []
        app.agent_process = None
        app.coding_future = Future()
        app.coding_state = coding_tools.CodingState()
        app.coding_state.coding_history = []
        app._save_coding_state = Mock()
        app.status = ""

        app._submit_code_only_task("Also update the tests")

        self.assertEqual(app.code_only_queue, ["Also update the tests"])
        self.assertIn("Queued", app.status)
        self.assertEqual(app.coding_state.coding_history, [])

        app.coding_future = None
        app.agent_process = None
        app.coding_state.job_patch = "reviewable patch"
        app._start_code_only_task = Mock()
        app._start_next_code_only_task()
        app._start_code_only_task.assert_not_called()

        app.coding_state.job_patch = ""
        app._start_code_only_task.return_value = True
        app._start_next_code_only_task()
        app._start_code_only_task.assert_called_once_with("Also update the tests")
        self.assertEqual(app.code_only_queue, [])

    def test_applying_patch_resumes_queued_code_only_task(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.coding_only_mode = True
        app.code_only_queue = ["Continue with the next task"]
        app.agent_process = None
        app.coding_future = Future()
        app.coding_future.set_result(None)
        app.coding_future_kind = "apply_patch"
        app.coding_state = coding_tools.CodingState(
            job_patch="applied patch",
            job_status="reviewing",
        )
        app._start_code_only_task = Mock(return_value=True)
        app._save_coding_state = Mock()
        app.coding_scroll_offset = 0

        app._finish_coding_request()

        app._start_code_only_task.assert_called_once_with(
            "Continue with the next task"
        )
        self.assertEqual(app.code_only_queue, [])
        self.assertEqual(app.coding_state.job_status, "applied")

    def test_confirm_mode_starts_agent_only_after_confirmation(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.coding_state = coding_tools.CodingState(coding_mode="confirm")
        app.coding_state.coding_history = []
        app.coding_future = Future()
        app.coding_future.set_result("Implement the requested fix.")
        app.coding_future_kind = "plan"
        app.view_mode = "chat"
        app.coding_scroll_offset = 0
        app.status = ""
        app._save_coding_state = Mock()
        app._confirm_agent_run = Mock(return_value=False)
        app._run_agent = Mock()

        app._finish_coding_request()

        app._confirm_agent_run.assert_called_once_with()
        app._run_agent.assert_not_called()
        self.assertEqual(app.coding_state.plan_status, "reviewing")

    def test_command_proposal_requires_approval_before_execution(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.coding_state = coding_tools.CodingState()
        app.coding_future = Future()
        app.coding_future.set_result("python -c 'print(1)'")
        app.workspace = Path("/workspace")
        app.command_confirmation = False
        app.agent_process = None
        app.coding_future_kind = ""
        app.coding_future_kind = "command"
        app.view_mode = "chat"
        app.coding_scroll_offset = 0
        app.status = ""
        app._save_coding_state = lambda: None

        app._finish_coding_request()
        self.assertTrue(app.command_confirmation)
        self.assertEqual(
            app.coding_state.proposed_command,
            "python -c 'print(1)'",
        )

        app.executor = Mock()
        app.executor.submit.return_value = Future()
        app.coding_future = None
        app._approve_command()
        app.executor.submit.assert_called_once_with(
            coding_tools.run_command,
            ["python", "-c", "print(1)"],
            Path("/workspace"),
        )

    def test_workspace_command_changes_root_and_persists_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "project folder"
            project.mkdir()
            app = ChatApp.__new__(ChatApp)
            app.workspace = Path("/previous")
            app.coding_state = coding_tools.CodingState()
            app.coding_future = None
            app.agent_process = None
            app.command_confirmation = False
            app.skills = {}
            app._save_coding_state = Mock()

            app._select_workspace(shlex.quote(str(project)))

            self.assertEqual(app.workspace, project.resolve())
            self.assertEqual(app.coding_state.workspace, str(project.resolve()))
            self.assertIn(str(project.resolve()), app.status)
            app._save_coding_state.assert_called_once_with()

    def test_saved_workspace_is_used_on_next_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "saved project"
            project.mkdir()
            data_file = root / "conversations.json"
            coding_tools.save_coding_state(
                data_file.with_name("coding_state.json"),
                coding_tools.CodingState(workspace=str(project)),
            )
            with (
                patch("Ai.chat_app.load_sessions", return_value=[]),
                patch.object(ChatApp, "_setup_screen"),
                patch.object(ChatApp, "_set_startup_status"),
            ):
                app = ChatApp(Mock(), "http://localhost", data_file)
            try:
                self.assertEqual(app.workspace, project.resolve())
            finally:
                app.executor.shutdown(wait=True)

    def test_profiles_round_trip_and_selected_profile_starts_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "chat_profiles.json"
            store = ProfileStore(
                profiles={
                    "Friendly cat": ChatProfile(
                        player_name="Display_Cat",
                        persona="You are a friendly assistant.",
                        chat_log="Player: Hello\nAI: Welcome back!",
                    )
                },
                selected="Friendly cat",
            )
            save_profiles(path, store)
            self.assertEqual(load_profiles(path), store)

            app = ChatApp.__new__(ChatApp)
            app.profile_store = store
            app.executor = Mock()
            app.executor.submit.return_value = Future()
            app.base_url = "http://localhost"
            app.status = ""
            app.pending_kind = ""
            app.pending_session_data = None
            app.pending_initial_chat_log = None
            app._prompt = Mock(side_effect=["", "", "", ""])
            app.chat_skill_library = {}

            app.create_session()

            self.assertEqual(
                app.pending_session_data,
                ("Display_Cat", "You are a friendly assistant."),
            )
            app.executor.submit.assert_called_once_with(
                unittest.mock.ANY,
                "http://localhost",
                "Display_Cat",
                "You are a friendly assistant.",
                "Player: Hello\nAI: Welcome back!",
                None,
                "",
                "",
                {},
            )

    def test_legacy_profile_without_chat_log_loads_with_empty_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "chat_profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "selected": "Old",
                        "profiles": {
                            "Old": {
                                "player_name": "Alex",
                                "persona": "Friendly",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = load_profiles(path)
        self.assertEqual(store.profiles["Old"].chat_log, "")

    def test_profile_save_captures_and_persists_active_chat_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = ChatApp.__new__(ChatApp)
            app.sessions = [
                SimpleNamespace(
                    player_name="Alex",
                    persona="Friendly helper",
                    chat_log="Player: Hello\nAI: Hi there!",
                )
            ]
            app.active_index = 0
            app.profile_store = ProfileStore(profiles={})
            app.profiles_path = Path(temp_dir) / "chat_profiles.json"
            app.status = ""

            app._handle_profile_command("save test-profile")

            loaded = load_profiles(app.profiles_path)
            self.assertEqual(
                loaded.profiles["test-profile"].chat_log,
                "Player: Hello\nAI: Hi there!",
            )

    def test_chat_log_can_be_loaded_from_saved_session_or_profile(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.sessions = [
            SimpleNamespace(chat_log="from saved session"),
        ]
        app.profile_store = ProfileStore(
            profiles={
                "Profile With Log": ChatProfile("Alex", "Friendly", "from profile")
            }
        )
        app.pending_initial_chat_log = None
        app.status = ""

        app._handle_log_command("load session 1")
        self.assertEqual(app.pending_initial_chat_log, "from saved session")
        app._handle_log_command('load profile "Profile With Log"')
        self.assertEqual(app.pending_initial_chat_log, "from profile")
        app._handle_log_command("clear")
        self.assertIsNone(app.pending_initial_chat_log)

    def test_profile_chat_log_is_displayed_and_sent_when_staged(self) -> None:
        profile = ChatProfile(
            "Alex",
            "Friendly",
            "Alex: Earlier conversation\nAI: Welcome back.",
        )
        app = ChatApp.__new__(ChatApp)
        app.sessions = []
        app.active_index = -1
        app.profile_store = ProfileStore(
            profiles={"Friends": profile},
            selected="Friends",
        )
        app.pending_initial_chat_log = None
        app.status = ""

        def panel_text(lines: list[list[tuple[str, str]]]) -> str:
            return " ".join(
                " ".join(segment[0] for line in lines for segment in line).split()
            )

        profile_lines = app._chat_panel_lines(80)
        self.assertIn("Earlier conversation", panel_text(profile_lines))

        app._handle_log_command("load profile Friends")
        staged_lines = app._chat_panel_lines(80)
        self.assertIn("STARTING CHAT LOG", panel_text(staged_lines))
        app.executor = Mock()
        app.executor.submit.return_value = Future()
        app.base_url = "http://localhost"
        app.pending_kind = ""
        app.pending_session_data = None
        app._prompt = Mock(side_effect=["", "", "", ""])
        app.chat_skill_library = {}

        app.create_session()

        app.executor.submit.assert_called_once_with(
            unittest.mock.ANY,
            "http://localhost",
            "Alex",
            "Friendly",
            profile.chat_log,
            None,
            "",
            "",
            {},
        )

    def test_explicitly_loaded_session_log_is_sent_to_new_session(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.profile_store = ProfileStore(profiles={})
        app.pending_initial_chat_log = "Previously saved conversation"
        app.executor = Mock()
        app.executor.submit.return_value = Future()
        app.base_url = "http://localhost"
        app.status = ""
        app._prompt = Mock(
            side_effect=["Alex", "Friendly assistant", "casual", "tutor, mine", "", ""]
        )
        app.chat_skill_library = {"mine": "My own skill."}
        app.pending_kind = ""
        app.pending_session_data = None

        app.create_session()

        app.executor.submit.assert_called_once_with(
            unittest.mock.ANY,
            "http://localhost",
            "Alex",
            "Friendly assistant",
            "Previously saved conversation",
            None,
            "",
            STYLE_PRESETS["casual"],
            {"tutor": CHAT_SKILLS["tutor"], "mine": "My own skill."},
        )

    def test_starting_chat_log_over_server_limit_is_rejected_explicitly(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.executor = Mock()
        app.status = ""

        app._start_session(
            "Alex",
            "Friendly assistant",
            "x" * (MAX_INITIAL_CHAT_LOG_LENGTH + 1),
        )

        app.executor.submit.assert_not_called()
        self.assertIn("server limit", app.status)

    def test_message_retry_backoff_increases_and_can_be_cancelled(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.pending_message = "hello"
        app.pending_message_session_id = 42
        app.pending_message_coding_context = ""
        app.retry_delay_seconds = 5
        app.retry_due = None
        app.status = ""
        app._confirm_message_retry = Mock(return_value=True)
        with patch("Ai.chat_app.time.monotonic", return_value=100):
            app._offer_message_retry(ServerError("network down"))
        self.assertEqual(app.retry_due, 105)
        self.assertEqual(app.retry_delay_seconds, 10)

        app.retry_due = None
        with patch("Ai.chat_app.time.monotonic", return_value=200):
            app._offer_message_retry(ServerError("network still down"))
        self.assertEqual(app.retry_due, 210)
        self.assertEqual(app.retry_delay_seconds, 15)

        app._cancel_message_retry()
        self.assertEqual(app.pending_message, "")
        self.assertIsNone(app.pending_message_session_id)
        self.assertIsNone(app.retry_due)
        self.assertEqual(app.status, "Cancelled pending message retry")

    def test_due_message_retry_resends_same_message_to_same_session(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.retry_due = 105
        app.pending_message = "hello"
        app.pending_message_session_id = 42
        app.pending_message_coding_context = ""
        app.pending_kind = ""
        app.base_url = "http://localhost"
        app.sessions = [
            ChatSession(
                local_id="session",
                player_name="Alex",
                persona="Friendly",
                chat_log="",
                server_session_id=42,
                access_token="session-token",
                server_url="",
            )
        ]
        app.ui_preferences = UIPreferences()
        app.status = ""
        app.executor = Mock()
        app.executor.submit.return_value = Future()
        with patch("Ai.chat_app.time.monotonic", return_value=105):
            app._start_due_message_retry()
        self.assertIsNotNone(app.future)
        self.assertEqual(app.pending_kind, "message_retry")
        self.assertEqual(app.status, "Retrying message…")
        app.executor.submit.assert_called_once_with(
            unittest.mock.ANY,
            "http://localhost",
            42,
            "hello",
            "session-token",
            "",
            True,
        )

    def test_workspace_cannot_change_with_agent_patch_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = ChatApp.__new__(ChatApp)
            app.workspace = Path("/previous")
            app.coding_state = coding_tools.CodingState(job_patch="unapplied patch")
            app.coding_future = None
            app.agent_process = None
            app.command_confirmation = False
            app.skills = {}
            app._save_coding_state = Mock()

            app._select_workspace(temp_dir)

            self.assertEqual(app.workspace, Path("/previous"))
            self.assertTrue(app.status.startswith("Error:"))
            app._save_coding_state.assert_not_called()

    def test_suggest_handles_skills_missing_from_selected_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = ChatApp.__new__(ChatApp)
            app.coding_future = None
            app.coding_state = coding_tools.CodingState(skills=["old-workspace-skill"])
            app.skills = {}
            app.workspace = Path(temp_dir)
            app.status = ""
            app.executor = Mock()
            app._save_coding_state = Mock()

            app._suggest_task()

        self.assertTrue(app.status.startswith("Error: Unknown skill"))
        self.assertEqual(app.coding_state.plan_status, "idle")
        app.executor.submit.assert_not_called()

    def test_failed_agent_launch_keeps_reviewed_plan_runnable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = ChatApp.__new__(ChatApp)
            app.coding_state = coding_tools.CodingState(
                plan="Implement the change",
                plan_workspace=temp_dir,
                plan_status="reviewing",
            )
            app.skills = {}
            app.workspace = Path(temp_dir)
            app.agent_process = None
            app.coding_future = Future()
            app.status = ""
            app._save_coding_state = Mock()

            app._run_agent()

        self.assertEqual(app.coding_state.plan_status, "reviewing")
        self.assertTrue(app.status.startswith("Error: wait for the active coding task"))

    def test_coding_plan_is_reviewed_before_agent_task_is_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = ChatApp.__new__(ChatApp)
            app.coding_state = coding_tools.CodingState(
                provider="custom",
                command_template=f"{shlex.quote(sys.executable)} -c {{prompt}}",
                skills=[],
            )
            app.coding_only_mode = False
            app.workspace = Path(temp_dir)
            app.sessions = []
            app.active_index = -1
            app.skills = coding_tools.discover_skills(app.workspace)
            app.coding_future = None
            app.coding_future_kind = ""
            app.coding_scroll_offset = 0
            app.view_mode = "chat"
            app.status = ""
            app.agent_process = None
            app.executor = Mock()
            app.executor.submit.return_value = Future()
            app._save_coding_state = lambda: None
            app.coding_request_chat_context = ""

            app._start_plan("Add a safe health endpoint")
            self.assertEqual(app.coding_state.plan_status, "planning")
            app.coding_future.set_result("Add route, then test it.")
            app._finish_coding_request()
            self.assertEqual(app.coding_state.plan_status, "reviewing")

            app.executor.reset_mock()
            app._run_agent()
            self.assertEqual(app.coding_state.plan_status, "submitted")
            app.executor.submit.assert_called_once_with(
                coding_tools.AgentProcess,
                app.coding_state.command_template,
                unittest.mock.ANY,
                app.workspace,
            )
            agent_prompt = app.executor.submit.call_args.args[2]
            self.assertNotIn("NORMAL CHAT PRIVATE", agent_prompt)

    def test_auto_mode_starts_agent_after_brief_without_user_run_command(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.coding_state = coding_tools.CodingState(coding_mode="auto")
        app.coding_state.coding_history = []
        app.coding_future = Future()
        app.coding_future.set_result("Implement the requested fix.")
        app.coding_future_kind = "plan"
        app.view_mode = "chat"
        app.coding_scroll_offset = 0
        app.status = ""
        app._save_coding_state = Mock()
        app._run_agent = Mock()

        app._finish_coding_request()

        app._run_agent.assert_called_once_with()
        self.assertEqual(app.coding_state.plan_status, "reviewing")

    def test_command_environment_removes_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "hidden",
                "GITHUB_TOKEN": "hidden",
                "APP_SETTING": "visible",
            },
            clear=False,
        ):
            environment = coding_tools.command_environment()
        self.assertNotIn("GEMINI_API_KEY", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertEqual(environment["APP_SETTING"], "visible")


if __name__ == "__main__":
    unittest.main()
