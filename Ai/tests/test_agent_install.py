import subprocess
import unittest
from unittest.mock import patch

from Ai import coding_tools


def fake_which(available: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in available else None


class InstallPlanTests(unittest.TestCase):
    def test_npm_provider_uses_fixed_command_when_npm_present(self) -> None:
        with patch("Ai.coding_tools.shutil.which", fake_which({"npm"})):
            plan = coding_tools.install_plan("claude")
        self.assertEqual(plan, [["npm", "install", "-g", "@anthropic-ai/claude-code"]])

    def test_missing_npm_is_installed_first_with_homebrew(self) -> None:
        with patch("Ai.coding_tools.shutil.which", fake_which({"brew"})):
            plan = coding_tools.install_plan("codex")
        self.assertEqual(plan[0], ["brew", "install", "node"])
        self.assertEqual(plan[1], ["npm", "install", "-g", "@openai/codex"])

    def test_missing_npm_uses_termux_pkg(self) -> None:
        with patch("Ai.coding_tools.shutil.which", fake_which({"pkg"})):
            plan = coding_tools.install_plan("copilot")
        self.assertEqual(plan[0], ["pkg", "install", "-y", "nodejs"])

    def test_missing_prerequisite_without_package_manager_raises(self) -> None:
        with patch("Ai.coding_tools.shutil.which", fake_which(set())):
            with self.assertRaisesRegex(ValueError, "npm"):
                coding_tools.install_plan("gemini")

    def test_providers_without_installer_stay_manual(self) -> None:
        for provider in ("cursor-agent", "custom"):
            with self.subTest(provider=provider):
                with self.assertRaises(ValueError):
                    coding_tools.install_plan(provider)

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown"):
            coding_tools.install_plan("nope")

    def test_every_install_step_belongs_to_a_known_provider(self) -> None:
        self.assertLessEqual(
            set(coding_tools.INSTALL_STEPS), set(coding_tools.PROVIDER_SPECS)
        )


class RunInstallTests(unittest.TestCase):
    def test_runs_steps_in_order_and_reports_success(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

        with patch("Ai.coding_tools.shutil.which", fake_which({"brew", "npm"})), patch(
            "Ai.coding_tools.subprocess.run", fake_run
        ):
            code, output = coding_tools.run_install(
                [["brew", "install", "node"], ["npm", "install", "-g", "x"]]
            )
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in calls], ["/usr/bin/brew", "/usr/bin/npm"])
        self.assertIn("$ npm install -g x", output)

    def test_stops_at_first_failure(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="EACCES")

        with patch("Ai.coding_tools.shutil.which", fake_which({"brew", "npm"})), patch(
            "Ai.coding_tools.subprocess.run", fake_run
        ):
            code, output = coding_tools.run_install(
                [["brew", "install", "node"], ["npm", "install", "-g", "x"]]
            )
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("EACCES", output)

    def test_missing_tool_returns_127(self) -> None:
        with patch("Ai.coding_tools.shutil.which", fake_which(set())):
            code, output = coding_tools.run_install([["npm", "install", "-g", "x"]])
        self.assertEqual(code, 127)
        self.assertIn("npm", output)

    def test_timeout_raises_runtime_error(self) -> None:
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)

        with patch("Ai.coding_tools.shutil.which", fake_which({"npm"})), patch(
            "Ai.coding_tools.subprocess.run", fake_run
        ):
            with self.assertRaisesRegex(RuntimeError, "limit"):
                coding_tools.run_install([["npm", "install", "-g", "x"]])

    def test_secrets_are_not_passed_to_installers(self) -> None:
        seen: dict[str, str] = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch.dict("os.environ", {"NPM_TOKEN": "secret", "PATH": "/usr/bin"}), patch(
            "Ai.coding_tools.shutil.which", fake_which({"npm"})
        ), patch("Ai.coding_tools.subprocess.run", fake_run):
            coding_tools.run_install([["npm", "install", "-g", "x"]])
        self.assertNotIn("NPM_TOKEN", seen)
        self.assertIn("PATH", seen)


class InstallCommandTests(unittest.TestCase):
    def make_app(self, confirmed: bool):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from Ai.chat_app import ChatApp

        app = ChatApp.__new__(ChatApp)
        app.agent_process = None
        app.coding_future = None
        app.coding_future_kind = ""
        app.status = ""
        app.coding_state = coding_tools.CodingState()
        app._confirm_agent_install = Mock(return_value=confirmed)
        app._select_tab = Mock()
        app._start_coding_request = Mock(return_value=True)
        app._save_coding_state = Mock()
        return app, SimpleNamespace()

    def test_confirmed_install_starts_background_request(self) -> None:
        app, _ = self.make_app(True)
        with patch("Ai.coding_tools.shutil.which", fake_which({"npm"})):
            app._handle_agent_command(["install", "claude"])
        app._start_coding_request.assert_called_once()
        kind, function, plan = app._start_coding_request.call_args.args
        self.assertEqual(kind, "agent_install")
        self.assertIs(function, coding_tools.run_install)
        self.assertEqual(plan, [["npm", "install", "-g", "@anthropic-ai/claude-code"]])

    def test_declined_install_runs_nothing(self) -> None:
        app, _ = self.make_app(False)
        with patch("Ai.coding_tools.shutil.which", fake_which({"npm"})):
            app._handle_agent_command(["install", "claude"])
        app._start_coding_request.assert_not_called()
        self.assertEqual(app.status, "Installation cancelled")

    def test_manual_only_provider_shows_website_without_prompting(self) -> None:
        app, _ = self.make_app(True)
        app._handle_agent_command(["install", "cursor-agent"])
        app._confirm_agent_install.assert_not_called()
        app._start_coding_request.assert_not_called()
        self.assertIn("https://docs.cursor.com/", app.coding_state.job_output)


if __name__ == "__main__":
    unittest.main()
