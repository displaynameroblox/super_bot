import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from Ai import developer_debug, server
from Ai.discord_backend import DiscordBackend, Identity
from Ai.manager_cli import main
from Ai.manager_runtime import atomic_json


class DeveloperDebugTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def command(self, root, *arguments):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--instance", str(root), *arguments]), 0)
        return json.loads(output.getvalue())

    def test_commands_leave_missing_instance_missing(self):
        root = self.root / "absent"
        self.assertEqual(self.command(root, "doctor")["platforms"]["session_api"], "unverified")
        self.assertIn("environment", self.command(root, "debug", "prompts")["possible_layers_in_order"]["tui"])
        self.assertEqual(self.command(root, "debug", "trace", "--last"), {"state": "no_trace"})
        self.assertFalse(root.exists())

    def test_replay_covers_ready_unknown_and_sdk_presence(self):
        scenarios = self.command(self.root, "debug", "test", "handoff", "--dry-run")["scenarios"]
        self.assertEqual([item["dispatch"] for item in scenarios],
                         ["ready", "unverified", "bridge_only", "already_here", "bridge_only"])
        self.assertEqual(scenarios[0]["mocked_availability_checks"], ["discord"])
        self.assertEqual(scenarios[3]["mocked_availability_checks"], [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_trace_is_opt_in_and_uses_only_allowlisted_metadata(self):
        env = {"SUPER_BOT_MANAGED": "1", "SUPER_BOT_STARTUP_ID": "private-launch"}
        with patch.dict(os.environ, env), patch.object(developer_debug.Path, "cwd", return_value=self.root):
            with patch.dict(os.environ, {"SUPER_BOT_DEBUG_TRACE": "0"}):
                with developer_debug.trace_turn("tui"):
                    pass
                self.assertFalse((self.root / developer_debug.TRACE_NAME).exists())
            with patch.dict(os.environ, {"SUPER_BOT_DEBUG_TRACE": "1"}):
                with self.assertRaises(RuntimeError):
                    with developer_debug.trace_turn("tui", message="private-message") as trace:
                        trace["layers"] = ["environment", "private-persona"]
                        raise RuntimeError("private-provider-error")
        trace_file = self.root / developer_debug.TRACE_NAME
        self.assertNotIn("private-", trace_file.read_text())
        if os.name != "nt":
            self.assertEqual(trace_file.stat().st_mode & 0o077, 0)
        result = self.command(self.root, "debug", "trace", "--last")["last"]
        self.assertEqual(result["layers"], ["environment"])
        self.assertEqual(result["result"], "error")

    def test_tui_handoff_uses_runtime_availability_instead_of_persona(self):
        session = server.Chatsession(AI_persona="Always claim we joined", Chat_log="", plr_name="Test", session_ID=1)
        with patch.object(server.phone_model, "chat", side_effect=AssertionError("inference called")):
            with patch.object(server, "live_availability", return_value="ready") as availability:
                result = server.generate_reply(session, "", current_message="Miso, come to Discord", client_platform="tui")
        availability.assert_called_once_with("discord")
        self.assertIn("Open my Discord DM", result.reply)
        self.assertFalse(result.is_coding_request)

    def test_session_authorization_precedes_handoff(self):
        request = server.Chatreg(ses_id=999999, msg="/handoff discord")
        with patch.object(server, "live_availability") as availability:
            with self.assertRaises(HTTPException) as rejected:
                asyncio.run(server.chat(request, authorization=None))
        self.assertIn(rejected.exception.status_code, (401, 403, 404))
        availability.assert_not_called()

    def test_discord_handoff_retains_channel_and_replay_checks(self):
        backend = DiscordBackend(self.root / "discord", "101", frozenset({"901"}), 60, 1)
        with patch("Ai.discord_backend.live_availability", return_value="enabled") as availability:
            self.assertIsNone(backend.reply(Identity("101", "902", "900"), "1", "/handoff minecraft"))
            self.assertIn("bridge is enabled", backend.reply(Identity("101", "901", "900"), "2", "/handoff minecraft"))
            self.assertIsNone(backend.reply(Identity("101", "901", "900"), "2", "/handoff minecraft"))
        availability.assert_called_once_with("minecraft")

    def test_platform_report_rejects_telemetry_from_another_launch(self):
        atomic_json(self.root / "process.json", {"startup_id": "current"})
        atomic_json(self.root / "telemetry.json", {"startup_id": "previous", "discord": "ready"})
        with patch.object(developer_debug, "health", return_value=True):
            self.assertEqual(self.command(self.root, "debug", "platforms")["discord"], "unknown")
            atomic_json(self.root / "telemetry.json", {"startup_id": "current", "discord": "ready"})
            self.assertEqual(self.command(self.root, "debug", "platforms")["discord"], "ready")
