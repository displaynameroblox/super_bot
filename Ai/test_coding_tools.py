import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from Ai import coding_tools
from Ai.chat_app import (
    ChatApp,
    ChatProfile,
    ChatSession,
    MAX_INITIAL_CHAT_LOG_LENGTH,
    ProfileStore,
    ServerError,
    normalize_server_url,
    request_json,
    load_profiles,
    load_sessions,
    save_profiles,
    save_sessions,
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

    def test_server_address_normalization_and_bearer_header(self) -> None:
        self.assertEqual(
            normalize_server_url("192.168.1.20:8000"),
            "http://192.168.1.20:8000",
        )
        with self.assertRaises(ValueError):
            normalize_server_url("http://user:password@example.com")

        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"{}"
        with patch("Ai.chat_app.urllib.request.urlopen", return_value=response) as urlopen:
            request_json(
                "http://192.168.1.20:8000",
                "/chat",
                {"msg": "hello"},
                access_token="opaque-token",
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer opaque-token")

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

    def test_gemini_planning_uses_brief_instruction_and_selected_skills(self) -> None:
        fake_response = SimpleNamespace(text="1. Change the feature\n2. Test it")
        generate_content = Mock(return_value=fake_response)
        fake_client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=generate_content
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(coding_tools, "gemini_client", return_value=fake_client):
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
        prompt = generate_content.call_args.kwargs["contents"]
        self.assertIn("Recent coding-mode request", prompt)
        self.assertIn("health-check project discussed earlier", prompt)
        self.assertIn("Recent normal-chat context:", prompt)

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

    def test_gemini_coding_detection_accepts_only_yes_or_no(self) -> None:
        with patch.object(coding_tools, "ask_gemini", return_value=" YES\n") as ask:
            self.assertTrue(
                coding_tools.detect_coding_request(
                    Path("/workspace"),
                    "Let's make a feature",
                    "## python\nUse tests.",
                )
            )
        self.assertIn("software", ask.call_args.args[1])
        self.assertIn("Recent normal-chat context:", ask.call_args.args[2])
        with patch.object(coding_tools, "ask_gemini", return_value="maybe"):
            with self.assertRaises(ValueError):
                coding_tools.detect_coding_request(
                    Path("/workspace"),
                    "Hello",
                    "No skills.",
                )

    def test_coding_history_limits_context_and_excludes_normal_chat(self) -> None:
        fake_response = SimpleNamespace(text="Implement the described feature.")
        fake_client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=Mock(return_value=fake_response)
            )
        )
        with patch.object(coding_tools, "gemini_client", return_value=fake_client):
            coding_tools.make_plan(
                Path("/workspace"),
                "Add feature",
                [{"role": "user", "content": "Coding discussion only"}],
                "## python\nUse tests.",
            )
        prompt = fake_client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("Coding discussion only", prompt)
        self.assertNotIn("normal chat transcript", prompt)

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
        app.coding_future = None
        app.coding_state = coding_tools.CodingState(
            coding_history=[{"role": "user", "content": "Earlier code task"}]
        )
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
            app._prompt = Mock(side_effect=["", ""])

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

    def test_explicitly_loaded_session_log_is_sent_to_new_session(self) -> None:
        app = ChatApp.__new__(ChatApp)
        app.profile_store = ProfileStore(profiles={})
        app.pending_initial_chat_log = "Previously saved conversation"
        app.executor = Mock()
        app.executor.submit.return_value = Future()
        app.base_url = "http://localhost"
        app.status = ""
        app._prompt = Mock(side_effect=["Alex", "Friendly assistant", "", ""])
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

    def test_coding_plan_is_reviewed_before_agent_task_is_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = ChatApp.__new__(ChatApp)
            app.coding_state = coding_tools.CodingState(
                provider="custom",
                command_template=f"{shlex.quote(sys.executable)} -c {{prompt}}",
                skills=[],
            )
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
