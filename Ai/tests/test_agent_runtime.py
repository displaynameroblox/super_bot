import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Ai import coding_tools


class AgentRuntimeTests(unittest.TestCase):
    def test_old_copilot_configuration_migrates_to_explicit_edit_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coding.json"
            path.write_text(json.dumps({"provider": "copilot", "command_template": "copilot -p {prompt}"}))
            state = coding_tools.load_coding_state(path)
            self.assertIn("--allow-tool=write", state.command_template)
            self.assertIn("--deny-tool=shell", state.command_template)
            custom = "my-cli --prompt {prompt}"
            path.write_text(json.dumps({"provider": "copilot", "command_template": custom}))
            self.assertEqual(coding_tools.load_coding_state(path).command_template, custom)

    def test_agent_gets_only_its_own_provider_credentials(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "copilot-auth", "GEMINI_API_KEY": "server-only", "ANTHROPIC_API_KEY": "claude-auth"}, clear=True):
            env = coding_tools.agent_environment(["copilot"], Path("/tmp"))
            self.assertEqual(env["GITHUB_TOKEN"], "copilot-auth")
            self.assertNotIn("GEMINI_API_KEY", env)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("GITHUB_TOKEN", coding_tools.agent_environment(["custom"], Path("/tmp")))

    def test_quota_and_login_failures_have_actionable_messages(self):
        self.assertIn("quota exceeded", coding_tools.agent_failure_reason("You have exceeded your monthly quota"))
        self.assertIn("login required", coding_tools.agent_failure_reason("Error: not authenticated"))
        self.assertEqual(coding_tools.agent_failure_reason("Changed two files successfully"), "")

    def test_missing_executable_gives_a_clear_error_before_copying(self):
        with self.assertRaisesRegex(FileNotFoundError, "Coding CLI.*missing"):
            coding_tools.AgentProcess("nonexistent-superbot-cli {prompt}", "task", Path("/tmp"))

    def test_copilot_shaped_cli_can_edit_then_produce_a_reviewable_patch(self):
        if os.name == "nt":
            self.skipTest("Executable fixture uses a POSIX shebang")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "copilot"
            executable.write_text(f"#!{sys.executable}\n" +
                "import sys\nfrom pathlib import Path\n" +
                "assert '--allow-tool=write' in sys.argv\n" +
                "Path('feature.txt').write_text('after\\n')\nprint('Edited feature.txt', flush=True)\n")
            executable.chmod(0o700)
            workspace = root / "project"
            workspace.mkdir()
            (workspace / "feature.txt").write_text("before\n")
            template = shlex.join([str(executable), *shlex.split(coding_tools.provider_template("copilot"))[1:]])
            process = coding_tools.AgentProcess(template, "Change feature.txt", workspace)
            process.reader.join(15)
            if process.reader.is_alive():
                process.cancel()
                self.fail("Agent did not finish")
            self.assertEqual(process.returncode, 0)
            self.assertIn("Edited feature.txt", "\n".join(process.collect_output()))
            self.assertIn("+after", process.patch)
            self.assertEqual((workspace / "feature.txt").read_text(), "before\n")
            coding_tools.apply_agent_patch(workspace, process.patch)
            self.assertEqual((workspace / "feature.txt").read_text(), "after\n")


if __name__ == "__main__":
    unittest.main()
