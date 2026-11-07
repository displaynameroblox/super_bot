import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Ai import chat_training


class ChatTrainingTests(unittest.TestCase):
    def test_collection_requires_explicit_server_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(chat_training.training_enabled())
        with patch.dict(os.environ, {"SUPER_BOT_COLLECT_CHAT_TRAINING": "1"}):
            self.assertTrue(chat_training.training_enabled())

    def test_exchange_is_private_and_exports_as_notebook_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "training" / "chats.sqlite"
            output = root / "training" / "user_chats.jsonl"
            chat_training.save_exchange(
                database,
                "Can you keep answers brief?",
                "Sure, I will be concise.",
            )
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            self.assertEqual(chat_training.export_exchanges(database, output), 1)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0]["messages"],
                [
                    {"role": "user", "content": "Can you keep answers brief?"},
                    {"role": "assistant", "content": "Sure, I will be concise."},
                ],
            )

    def test_export_fails_explicitly_for_missing_or_empty_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "missing.sqlite"
            output = Path(directory) / "user_chats.jsonl"
            with self.assertRaises(FileNotFoundError):
                chat_training.export_exchanges(database, output)
            chat_training.save_exchange(database, "Hi", "Hello")
            with patch("sqlite3.connect") as connection:
                connection.return_value.execute.return_value.fetchall.return_value = []
                with self.assertRaisesRegex(ValueError, "No chat exchanges"):
                    chat_training.export_exchanges(database, output)


if __name__ == "__main__":
    unittest.main()
