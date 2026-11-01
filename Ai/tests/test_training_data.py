import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from Ai import training_data


class TrainingDataTests(unittest.TestCase):
    def test_normalizes_role_aliases_and_redacts_basic_contact_patterns(self) -> None:
        messages = training_data.normalize_conversation(
            [
                {"role": "assistant", "content": "opening"},
                {"role": "human", "content": "Email me at alex@example.com"},
                {"role": "gpt", "content": "Sure, call +1 (555) 123-4567."},
            ]
        )
        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "Email me at [EMAIL REDACTED]"},
                {"role": "assistant", "content": "Sure, call [PHONE REDACTED]."},
            ],
        )
        self.assertIsNone(training_data.normalize_conversation([{"role": "user", "content": "No reply"}]))

    def test_instruction_rows_include_context(self) -> None:
        messages = training_data._instruction_pair(
            {
                "instruction": "Explain the bug",
                "input": "The loop never advances",
                "output": "Increment the loop variable.",
            }
        )
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("The loop never advances", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "Increment the loop variable.")

    def test_openassistant_parent_chain_becomes_a_dialogue(self) -> None:
        examples = list(
            training_data._oasst_conversations(
                [
                    {
                        "message_id": "u1",
                        "parent_id": None,
                        "role": "prompter",
                        "text": "What is FTS5?",
                        "lang": "en",
                    },
                    {
                        "message_id": "a1",
                        "parent_id": "u1",
                        "role": "assistant",
                        "text": "SQLite full-text search.",
                        "lang": "en",
                        "rank": 0,
                    },
                ]
            )
        )
        self.assertEqual(len(examples), 1)
        self.assertEqual([turn["role"] for turn in examples[0]], ["user", "assistant"])

    def test_pdf_dataset_loader_streams_selected_capped_rows(self) -> None:
        calls = []

        def fake_load_dataset(repo, **kwargs):
            calls.append((repo, kwargs))
            if repo == "databricks/databricks-dolly-15k":
                return [
                    {"instruction": f"Question {i}", "response": f"Answer {i}"}
                    for i in range(5)
                ]
            return [
                {
                    "language": "English",
                    "toxic": False,
                    "conversation": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi there."},
                    ],
                }
            ]

        datasets_module = types.ModuleType("datasets")
        datasets_module.load_dataset = fake_load_dataset
        with patch.dict(sys.modules, {"datasets": datasets_module}):
            examples = training_data.load_pdf_sft_examples(
                per_dataset_limit=2,
                selected_datasets=["dolly", "wildchat"],
            )
        self.assertEqual(len(examples), 3)
        self.assertEqual([example["source"] for example in examples], ["dolly", "dolly", "wildchat"])
        self.assertTrue(all(call[1]["streaming"] for call in calls))
        self.assertEqual(calls[0][1]["split"], "train")
        self.assertEqual(
            {call[0] for call in calls},
            {"databricks/databricks-dolly-15k", "allenai/WildChat-1M"},
        )

    def test_chat_export_loader_rejects_malformed_or_empty_examples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user_chats.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "messages": [
                            {"role": "user", "content": "I prefer short answers."},
                            {"role": "assistant", "content": "Understood."},
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            examples = training_data.load_chat_examples(str(path))
            self.assertEqual(examples[0]["source"], "opt-in-user-chat")
            path.write_text('{"messages":[]}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid opt-in user chat"):
                training_data.load_chat_examples(str(path))


if __name__ == "__main__":
    unittest.main()
