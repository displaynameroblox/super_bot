import json
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from Ai import server
from Ai.chat_app import (
    ChatApp,
    ChatSession,
    ServerError,
    load_chat_skills,
    load_sessions,
    parse_chat_skills,
    save_chat_skills,
    save_sessions,
    style_message_text,
)
from Ai.protocol import (
    CHAT_SKILLS,
    MAX_ACTIVE_CHAT_SKILLS,
    MAX_SKILL_TEXT_LENGTH,
    SKILL_NAME,
    normalize_skills,
)

REPLY = '{"reply": "Arr!", "is_coding_request": false}'


class ProtocolSkillTests(unittest.TestCase):
    def test_builtin_skills_are_valid(self) -> None:
        for name, text in CHAT_SKILLS.items():
            with self.subTest(name=name):
                self.assertRegex(name, SKILL_NAME)
                self.assertEqual(normalize_skills({name: text}), {name: text})

    def test_names_are_lowercased_and_whitespace_collapsed(self) -> None:
        self.assertEqual(
            normalize_skills({" Pirate ": "Talk   like\na pirate."}),
            {"pirate": "Talk like a pirate."},
        )

    def test_invalid_skills_are_rejected(self) -> None:
        for skills in (
            {"bad name!": "x"},
            {"ok": "   "},
            {"ok": "x" * (MAX_SKILL_TEXT_LENGTH + 1)},
            {"a": "x", "A": "y"},
            {f"s{i}": "x" for i in range(MAX_ACTIVE_CHAT_SKILLS + 1)},
        ):
            with self.subTest(skills=skills):
                with self.assertRaises(ValueError):
                    normalize_skills(skills)


class ServerSkillTests(unittest.TestCase):
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
        self.client = TestClient(server.app)

    def start(self, skills=None, password: str = "pw") -> dict:
        response = self.client.post(
            "/start_ses",
            json={
                "AI_persona": "a sailor",
                "plr_name": "Host",
                "password": password,
                "AI_skills": skills or {},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_skills_sent_at_session_start_are_used_without_registration(self) -> None:
        grant = self.start({"Pirate": "Talk like a pirate."})
        self.assertEqual(grant["AI_skills"], {"pirate": "Talk like a pirate."})
        prompt = self.phone.call_args.args[0][0]["content"]
        self.assertIn("Talk like a pirate.", prompt)

    def test_no_skill_section_without_skills(self) -> None:
        self.start()
        self.assertNotIn("Active chat skills", self.phone.call_args.args[0][0]["content"])

    def test_invalid_skills_at_start_are_rejected(self) -> None:
        for skills in (
            {"bad name!": "x"},
            {"ok": "x" * (MAX_SKILL_TEXT_LENGTH + 1)},
            {f"s{i}": "x" for i in range(MAX_ACTIVE_CHAT_SKILLS + 1)},
        ):
            with self.subTest(skills=skills):
                response = self.client.post(
                    "/start_ses",
                    json={"AI_persona": "p", "plr_name": "n", "AI_skills": skills},
                )
                self.assertEqual(response.status_code, 422)

    def test_guests_receive_the_skills_when_they_log_in(self) -> None:
        grant = self.start({"tutor": "Explain simply."})
        login = self.client.post(
            "/login", json={"session_ID": grant["session_ID"], "password": "pw"}
        )
        self.assertEqual(login.json()["AI_skills"], {"tutor": "Explain simply."})

    def test_only_the_host_can_replace_skills(self) -> None:
        grant = self.start({"tutor": "Explain simply."})
        sid = grant["session_ID"]
        host = {"Authorization": f"Bearer {grant['access_token']}"}
        guest_token = self.client.post(
            "/login", json={"session_ID": sid, "password": "pw"}
        ).json()["access_token"]
        guest = {"Authorization": f"Bearer {guest_token}"}
        body = {"ses_id": sid, "skills": {"pirate": "Arr."}}

        self.assertEqual(self.client.post("/skills", json=body).status_code, 401)
        self.assertEqual(
            self.client.post("/skills", json=body, headers=guest).status_code, 403
        )
        ok = self.client.post("/skills", json=body, headers=host)
        self.assertEqual(ok.json(), {"AI_skills": {"pirate": "Arr."}})
        cleared = self.client.post(
            "/skills", json={"ses_id": sid, "skills": {}}, headers=host
        )
        self.assertEqual(cleared.json(), {"AI_skills": {}})

    def test_invalid_replacement_is_rejected(self) -> None:
        grant = self.start()
        host = {"Authorization": f"Bearer {grant['access_token']}"}
        response = self.client.post(
            "/skills",
            json={"ses_id": grant["session_ID"], "skills": {"x": ""}},
            headers=host,
        )
        self.assertEqual(response.status_code, 422)

    def test_replaced_skills_reach_the_next_reply(self) -> None:
        grant = self.start()
        sid = grant["session_ID"]
        host = {"Authorization": f"Bearer {grant['access_token']}"}
        self.client.post(
            "/skills", json={"ses_id": sid, "skills": {"pirate": "Arr."}}, headers=host
        )
        self.client.post("/chat", json={"ses_id": sid, "msg": "hi"}, headers=host)
        self.assertIn("Arr.", self.phone.call_args.args[0][0]["content"])

    def test_unknown_session_returns_404(self) -> None:
        grant = self.start()
        with patch.dict(server.chat_sessions, {}, clear=True):
            with self.assertRaises(HTTPException) as caught:
                server.set_skills(
                    server.SkillsRequest(ses_id=grant["session_ID"], skills={}),
                    authorization=f"Bearer {grant['access_token']}",
                )
        self.assertEqual(caught.exception.status_code, 404)


class ClientStorageTests(unittest.TestCase):
    def test_library_round_trips_and_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_skills.json"
            self.assertEqual(load_chat_skills(path), {})
            save_chat_skills(path, {"pirate": "Arr."})
            self.assertEqual(load_chat_skills(path), {"pirate": "Arr."})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_library_file_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_skills.json"
            path.write_text(json.dumps(["nope"]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_chat_skills(path)

    def test_session_skills_are_saved_with_the_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_sessions.json"
            session = ChatSession(
                "a", "Alex", "p", "log", 1, "tok", "http://s", chat_skills={"t": "Teach."}
            )
            save_sessions(path, [session])
            self.assertEqual(load_sessions(path)[0].chat_skills, {"t": "Teach."})

    def write_session(self, directory: str, **extra) -> Path:
        path = Path(directory) / "chat_sessions.json"
        item = {
            "local_id": "a",
            "player_name": "Alex",
            "persona": "p",
            "chat_log": "",
            "server_session_id": 1,
            **extra,
        }
        path.write_text(json.dumps([item]), encoding="utf-8")
        return path

    def test_sessions_saved_before_chat_skills_existed_still_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_session(directory)
            self.assertEqual(load_sessions(path)[0].chat_skills, {})

    def test_malformed_session_skills_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_session(directory, chat_skills=["roleplay"])
            with self.assertRaises(ValueError):
                load_sessions(path)


class ParseChatSkillsTests(unittest.TestCase):
    def test_names_resolve_to_instructions(self) -> None:
        result = parse_chat_skills("Roleplay, mine", {**CHAT_SKILLS, "mine": "My text."})
        self.assertEqual(result, {"roleplay": CHAT_SKILLS["roleplay"], "mine": "My text."})

    def test_blank_means_no_skills_and_duplicates_collapse(self) -> None:
        self.assertEqual(parse_chat_skills("  ", CHAT_SKILLS), {})
        self.assertEqual(len(parse_chat_skills("tutor tutor, tutor", CHAT_SKILLS)), 1)

    def test_unknown_name_and_too_many_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "nope"):
            parse_chat_skills("tutor, nope", CHAT_SKILLS)
        many = {f"s{i}": "x" for i in range(MAX_ACTIVE_CHAT_SKILLS + 1)}
        with self.assertRaises(ValueError):
            parse_chat_skills(", ".join(many), many)


class ClientCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def make_app(self, session: ChatSession | None = None, library=None) -> ChatApp:
        app = ChatApp.__new__(ChatApp)
        app.sessions = [session] if session else []
        app.active_index = 0 if session else -1
        app.base_url = "http://localhost"
        app.executor = Mock()
        app.future = None
        app.pending_kind = ""
        app.status = ""
        app.chat_skills_path = Path(self.directory.name) / "chat_skills.json"
        app.chat_skill_library = dict(library or {})
        app.coding_state = Mock()
        app._select_tab = Mock()
        return app

    def session(self, skills=None) -> ChatSession:
        return ChatSession("l", "Alex", "p", "", 7, "tok", "http://srv", chat_skills=dict(skills or {}))

    def test_create_saves_locally_and_never_contacts_the_server(self) -> None:
        app = self.make_app()
        app._handle_chat_skill_command("create Pirate Talk   like a pirate")
        app.executor.submit.assert_not_called()
        self.assertEqual(app.chat_skill_library, {"pirate": "Talk like a pirate"})
        self.assertEqual(load_chat_skills(app.chat_skills_path), app.chat_skill_library)

    def test_delete_removes_only_your_skills(self) -> None:
        app = self.make_app(library={"pirate": "Arr."})
        app._handle_chat_skill_command("delete pirate")
        self.assertEqual(app.chat_skill_library, {})
        self.assertEqual(load_chat_skills(app.chat_skills_path), {})
        app._handle_chat_skill_command("delete roleplay")
        self.assertTrue(app.status.startswith("Error"))
        app._handle_chat_skill_command("delete ghost")
        self.assertTrue(app.status.startswith("Error"))

    def test_create_validation(self) -> None:
        for argument in (
            "create bad!name text",
            "create roleplay overwrite",
            "create onlyname",
            f"create toolong {'x' * (MAX_SKILL_TEXT_LENGTH + 1)}",
        ):
            with self.subTest(argument=argument):
                app = self.make_app()
                app._handle_chat_skill_command(argument)
                self.assertEqual(app.chat_skill_library, {})
                self.assertFalse(app.chat_skills_path.exists())

    def test_use_sends_the_complete_active_set_with_texts(self) -> None:
        app = self.make_app(self.session({"tutor": CHAT_SKILLS["tutor"]}), {"pirate": "Arr."})
        app._handle_chat_skill_command("use pirate")
        args = app.executor.submit.call_args.args
        self.assertEqual(
            args[1:],
            ("http://srv", 7, {"tutor": CHAT_SKILLS["tutor"], "pirate": "Arr."}, "tok"),
        )
        self.assertEqual(app.pending_kind, "chat_skill")

    def test_off_and_clear(self) -> None:
        app = self.make_app(self.session({"a": "x", "b": "y"}))
        app._handle_chat_skill_command("off a")
        self.assertEqual(app.executor.submit.call_args.args[3], {"b": "y"})
        app.future = None
        app._handle_chat_skill_command("clear")
        self.assertEqual(app.executor.submit.call_args.args[3], {})

    def test_activation_limit_and_unknown_names_are_rejected_locally(self) -> None:
        full = {f"s{i}": "x" for i in range(MAX_ACTIVE_CHAT_SKILLS)}
        app = self.make_app(self.session(full))
        app._handle_chat_skill_command("use tutor")
        self.assertTrue(app.status.startswith("Error"))
        app._handle_chat_skill_command("use nope")
        self.assertTrue(app.status.startswith("Error"))
        app.executor.submit.assert_not_called()

    def test_changes_need_a_session_but_listing_does_not(self) -> None:
        app = self.make_app()
        app._handle_chat_skill_command("use tutor")
        self.assertTrue(app.status.startswith("Error"))
        app._handle_chat_skill_command("list")
        self.assertIn("roleplay", app.status)
        app.executor.submit.assert_not_called()

    def test_list_marks_active_and_your_own_skills(self) -> None:
        app = self.make_app(self.session({"tutor": "x"}), {"pirate": "Arr."})
        app._handle_chat_skill_command("list")
        self.assertIn("tutor*", app.status)
        self.assertIn("pirate [yours]", app.status)
        self.assertNotIn("roleplay*", app.status)

    def test_show_displays_instructions(self) -> None:
        app = self.make_app(library={"pirate": "Arr."})
        app._handle_chat_skill_command("show pirate")
        self.assertIn("Arr.", app.coding_state.job_output)

    def test_finishing_a_change_updates_and_saves_the_session(self) -> None:
        session = self.session({"old": "x"})
        app = self.make_app(session)
        future: Future = Future()
        future.set_result({"AI_skills": {"pirate": "Arr."}})
        app.future, app.pending_kind = future, "chat_skill"
        app.data_file = Path(self.directory.name) / "chat_sessions.json"
        app.scroll_offset = 0
        with patch("Ai.chat_app.save_sessions") as save:
            app._finish_pending_request()
        self.assertEqual(session.chat_skills, {"pirate": "Arr."})
        save.assert_called_once()

    def test_non_host_error_is_shown_and_session_is_unchanged(self) -> None:
        session = self.session({"old": "x"})
        app = self.make_app(session)
        future: Future = Future()
        future.set_exception(
            ServerError("Server returned HTTP 403: host only", status_code=403)
        )
        app.future, app.pending_kind = future, "chat_skill"
        app._finish_pending_request()
        self.assertIn("403", app.status)
        self.assertEqual(session.chat_skills, {"old": "x"})

    def test_roleplay_actions_render_as_emphasis(self) -> None:
        segments = style_message_text("*I opened the door for you* come in")
        self.assertTrue(
            any(emphasised and "opened the door" in text for text, emphasised in segments)
        )


if __name__ == "__main__":
    unittest.main()
