import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Ai import database, phone_model, preflight


class PreflightTests(unittest.TestCase):
    def test_startup_does_not_run_development_suite_by_default(self):
        import os
        with patch.dict(os.environ, {"SUPER_BOT_RUN_TESTS": "0"}), patch.object(preflight, "run_tests") as run:
            self.assertTrue(preflight.ensure_tests_pass())
        run.assert_not_called()

    def test_diagnose_checks_database_and_model_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "super_bot.db"
            with (
                patch.object(database, "default_path", return_value=path),
                patch.object(
                    phone_model,
                    "provider_description",
                    return_value="Phone-hosted model (superbot-phone)",
                ),
                patch.object(phone_model, "probe") as probe,
            ):
                lines = preflight.diagnose()

        self.assertIn("Temporary server database ready (RAM only)", lines)
        self.assertFalse(path.exists())
        self.assertIn("AI provider ready: Phone-hosted model (superbot-phone)", lines)
        probe.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
