"""Interaction and terminal-size regressions for the curses interface."""

from concurrent.futures import Future
import curses
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from Ai import tui
from Ai.chat_app import ChatApp, ChatSession, TABS, parse_chat_log


class Screen:
    def __init__(self, height=30, width=120):
        self.height, self.width = height, width
        self.calls = []
        self.keys = iter(())

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.calls.clear()

    def addstr(self, y, x, text, attributes=0):
        assert 0 <= y < self.height and 0 <= x < self.width
        assert x + tui.width(text) < self.width, (y, x, text)
        self.calls.append((y, x, text, attributes))

    def move(self, y, x):
        assert 0 <= y < self.height and 0 <= x < self.width

    def refresh(self):
        pass

    def timeout(self, _):
        pass

    def get_wch(self):
        return next(self.keys)

    def text(self):
        rows = [[" "] * self.width for _ in range(self.height)]
        for y, x, text, _ in self.calls:
            for char in text:
                size = tui.cell_width(char)
                if size == 0:
                    if x:
                        rows[y][x - 1] += char
                    continue
                rows[y][x] = char
                if size == 2:
                    rows[y][x + 1] = ""
                x += size
        return "\n".join("".join(row).rstrip() for row in rows)


class TuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.screen = Screen()
        for target, kwargs in (
            ("Ai.chat_app.ChatApp._setup_screen", {}),
            ("Ai.chat_app.ChatApp._color", {"return_value": 0}),
            ("Ai.chat_app.curses.curs_set", {}),
            ("Ai.chat_app.transport.configure_trust_store", {}),
        ):
            mocker = patch(target, **kwargs)
            mocker.start()
            self.addCleanup(mocker.stop)
        self.app = ChatApp(self.screen, "https://localhost:8000", Path(self.temp.name) / "chats.json")
        self.addCleanup(self.app.executor.shutdown, wait=False, cancel_futures=True)
        self.app._start_session_events = Mock()
        self.app.sessions = [ChatSession(
            local_id="demo", player_name="Alex", persona="Helpful assistant",
            chat_log="[2026-11-04T12:30:00] Alex: Hello\n[2026-11-04T12:30:01] AI: Hi there!\n  ```python\n  print('hello')\n  ```",
            server_session_id=123,
        )]
        self.app.active_index = 0

    def test_all_views_and_palette_fit_small_and_large_terminals(self):
        for height, width in ((12, 40), (16, 50), (24, 80), (30, 120), (45, 180)):
            self.screen.height, self.screen.width = height, width
            for tab in TABS:
                with self.subTest(size=(height, width), tab=tab):
                    self.app._select_tab(tab)
                    self.app.draw()
                    self.assertIn("Message", self.screen.text())
                    self.assertEqual(len(self.app.tab_hit_targets), 5)
                    self.assertLess(self.app.tab_hit_targets[-1][2], width)
            self.app._toggle_split()
            self.app.draw()
            self.app._open_actions()
            self.app.draw()
            self.assertIn("QUICK ACTIONS", self.screen.text())
            self.app.action_menu_visible = False

    def test_mid_line_editing_delete_home_end_and_word_deletion(self):
        for key in "helo world":
            self.app.handle_key(key)
        self.app.handle_key(curses.KEY_HOME)
        for _ in range(3):
            self.app.handle_key(curses.KEY_RIGHT)
        self.app.handle_key("l")
        self.assertEqual(self.app.input_text, "hello world")
        self.app.handle_key(curses.KEY_END)
        self.app.handle_key("\x17")
        self.assertEqual(self.app.input_text, "hello ")
        self.app.handle_key(curses.KEY_HOME)
        self.app.handle_key(curses.KEY_DC)
        self.assertEqual(self.app.input_text, "ello ")
        self.assertEqual(self.app.active_tab, "Chat")

    def test_draft_survives_a_pending_reply_and_exit_is_available(self):
        self.app.future = Future()
        self.app.pending_kind = "message"
        self.app.submit_input = Mock()
        for key in "next question":
            self.app.handle_key(key)
        self.app.handle_key("\n")
        self.app.submit_input.assert_not_called()
        self.assertEqual(self.app.input_text, "next question")
        self.app.handle_key("\x03")
        self.assertTrue(self.app.closed)

    def test_palette_search_stages_apply_without_applying(self):
        self.app._apply_agent_patch = Mock()
        self.app.handle_key("\x10")
        for key in "/apply":
            self.app.handle_key(key)
        self.app.handle_key("\n")
        self.assertEqual(self.app.input_text, "/apply")
        self.app._apply_agent_patch.assert_not_called()
        self.assertFalse(self.app.action_menu_visible)

    def test_no_palette_matches_and_escape_preserve_draft(self):
        self.app.input_text = "Keep this draft"
        self.app._open_actions("no-such-action")
        self.app.draw()
        self.assertIn("No matches", self.screen.text())
        for key in (curses.KEY_DOWN, "\n", "\x1b"):
            self.app.handle_key(key)
        self.assertEqual(self.app.input_text, "Keep this draft")

    def test_conversation_picker_uses_saved_session_without_sending_draft(self):
        self.app.handle_key("\x0f")
        self.assertEqual(len(self.app._filtered_actions()), 1)
        self.app.input_text = "unsent draft"
        self.app.handle_key("\n")
        self.app._start_session_events.assert_called_once()
        self.assertEqual(self.app.input_text, "unsent draft")

    def test_palette_cannot_switch_session_during_reply(self):
        self.app.future = Future()
        self.app._open_actions("/open")
        self.app.handle_key("\n")
        self.app._start_session_events.assert_not_called()

    def test_startup_enter_creates_chat_and_join_masks_password(self):
        self.screen.keys = iter(["\n"])
        self.assertEqual(self.app._login_form(), ("NEW", ""))
        self.screen.keys = iter([*"123", "\t", *"secret", "\n"])
        self.assertEqual(self.app._login_form(), ("123", "secret"))
        self.assertNotIn("secret", self.screen.text())
        self.assertIn("******", self.screen.text())

    def test_prompt_handles_resize_and_password_cursor_edits(self):
        self.screen.height, self.screen.width = 12, 40
        self.screen.keys = iter([*"ac", curses.KEY_LEFT, "b", curses.KEY_RESIZE, "\n"])
        self.assertEqual(self.app._prompt("Password", secret=True), "abc")
        self.assertNotIn("abc", self.screen.text())

    def test_review_uses_distinct_added_and_removed_styles(self):
        self.app.coding_state.job_patch = "--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n"
        lines = self.app._review_panel_lines()
        self.assertIn([("-old", "error")], lines)
        self.assertIn([("+new", "success")], lines)
        self.assertIn([("+++ b/file", "player")], lines)

    def test_multiline_code_and_colons_do_not_become_fake_speakers(self):
        messages = parse_chat_log("AI: Here is an example:\n  Notes:\n  ```python\n  def hello():\n      return 1\n  ```")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].speaker, "AI")
        self.assertIn("def hello():\n    return 1", messages[0].text)

    def test_live_update_reads_full_local_history_instead_of_server_tail(self):
        self.app.sessions[0].chat_log = "Old messages\n" * 3000
        complete = self.app.sessions[0].chat_log + "New message and reply"
        self.app.session_events.put({"type": "message", "exchange_id": "new", "text": "New message", "reply": "New reply", "chat_log": "short server tail"})
        with patch("Ai.chat_app.client_data.local_history", return_value=complete), patch("Ai.chat_app.save_sessions"), patch.object(self.app, "_sync_profile_chat_log", return_value=True):
            self.app._poll_session_events()
        self.assertEqual(self.app.sessions[0].chat_log, complete)

    def test_review_starts_at_top_and_includes_large_patch(self):
        self.app.coding_state.job_patch = "--- a/file\n+++ b/file\n" + "\n".join(f"+line {i}" for i in range(1200))
        self.app._select_tab("Review")
        self.app.draw()
        self.assertIn("REVIEW AGENT PATCH", self.screen.text())
        self.assertIn([("+line 1199", "success")], self.app._review_panel_lines())

    def test_wide_characters_wrap_and_editor_caret_remains_visible(self):
        text = "hello 世界 café e\u0301 " * 10
        for columns in (1, 5, 20):
            visible, cursor = tui.editor_view(text, len(text), columns)
            self.assertLessEqual(tui.width(visible), columns)
            self.assertLess(cursor, columns)
            lines = self.app._wrap_rich_segments([(text, "normal")], columns)
            self.assertTrue(all(sum(tui.width(s) for s, _ in line) <= columns for line in lines))
        self.assertNotIn("\x1b", tui.clean("untrusted\x1b[31m"))


if __name__ == "__main__":
    unittest.main()
