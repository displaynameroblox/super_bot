"""Curated supervised fine-tuning loaders for the datasets listed in the PDF."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any


PDF_SFT_DATASETS: dict[str, dict[str, Any]] = {
    "openassistant": {
        "repo": "OpenAssistant/oasst1",
        "split": "train",
        "config": None,
    },
    "wildchat": {
        "repo": "allenai/WildChat-1M",
        "split": "train",
        "config": None,
    },
    "ultrachat": {
        "repo": "HuggingFaceH4/ultrachat_200k",
        "split": "train_sft",
        "config": None,
    },
    "smoltalk": {
        "repo": "HuggingFaceTB/smoltalk",
        "split": "train",
        "config": "SFT",
    },
    "no_robots": {
        "repo": "HuggingFaceH4/no_robots",
        "split": "train",
        "config": None,
    },
    "dolly": {
        "repo": "databricks/databricks-dolly-15k",
        "split": "train",
        "config": None,
    },
    "codealpaca": {
        "repo": "HuggingFaceH4/CodeAlpaca_20K",
        "split": "train",
        "config": None,
    },
    "evol_code": {
        "repo": "nickrosh/Evol-Instruct-Code-80k-v1",
        "split": "train",
        "config": None,
    },
}

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d().\-\s]{7,}\d)(?!\w)")


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = EMAIL_RE.sub("[EMAIL REDACTED]", value)
    return PHONE_RE.sub("[PHONE REDACTED]", value).strip()


def _role_message(row: Any) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    role = row.get("role", row.get("from", row.get("speaker")))
    content = row.get("content", row.get("text", row.get("value")))
    role_map = {
        "human": "user",
        "prompter": "user",
        "user": "user",
        "gpt": "assistant",
        "model": "assistant",
        "assistant": "assistant",
    }
    mapped_role = role_map.get(role.casefold()) if isinstance(role, str) else None
    cleaned = _clean(content)
    if mapped_role and cleaned:
        return {"role": mapped_role, "content": cleaned}
    return None


def normalize_conversation(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list):
        return None
    messages = [message for row in value if (message := _role_message(row))]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    if len(messages) < 2 or messages[-1]["role"] != "assistant":
        return None
    return messages


def _instruction_pair(row: dict[str, Any]) -> list[dict[str, str]] | None:
    instruction = _clean(row.get("instruction", row.get("prompt", "")))
    response = _clean(
        row.get("response", row.get("output", row.get("completion", "")))
    )
    context = _clean(row.get("context", row.get("input", "")))
    if not instruction or not response:
        return None
    if context:
        instruction = f"{instruction}\n\nContext:\n{context}"
    return [
        {"role": "user", "content": instruction},
        {"role": "assistant", "content": response},
    ]


def _wildchat_conversation(row: dict[str, Any]) -> list[dict[str, str]] | None:
    if row.get("toxic") is True or row.get("language") not in {None, "English"}:
        return None
    return normalize_conversation(row.get("conversation", row.get("messages")))


def _row_conversation(row: dict[str, Any]) -> list[dict[str, str]] | None:
    messages = normalize_conversation(row.get("messages"))
    if messages:
        return messages
    conversations = row.get("conversation")
    messages = normalize_conversation(conversations)
    if messages:
        return messages
    return _instruction_pair(row)


def _oasst_conversations(rows: Iterable[dict[str, Any]]) -> Iterator[list[dict[str, str]]]:
    indexed = {
        row.get("message_id"): row
        for row in rows
        if isinstance(row.get("message_id"), str)
    }
    for row in indexed.values():
        if row.get("role") != "assistant":
            continue
        if row.get("deleted") is True or row.get("review_result") is False:
            continue
        if row.get("rank") not in (None, 0) or row.get("lang") not in (None, "en"):
            continue
        chain = []
        current = row
        seen_ids: set[str] = set()
        while current is not None and len(chain) < 12:
            message_id = current.get("message_id")
            if not isinstance(message_id, str) or message_id in seen_ids:
                break
            seen_ids.add(message_id)
            chain.append(current)
            parent_id = current.get("parent_id")
            current = indexed.get(parent_id) if isinstance(parent_id, str) else None
        chain.reverse()
        messages = normalize_conversation(
            [
                {"role": item.get("role"), "content": item.get("text")}
                for item in chain
            ]
        )
        if messages:
            yield messages


def load_pdf_sft_examples(
    *,
    per_dataset_limit: int = 200,
    selected_datasets: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    if per_dataset_limit < 1:
        raise ValueError("per_dataset_limit must be positive.")
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "Install the notebook training dependencies, including datasets."
        ) from exc

    selected = list(
        PDF_SFT_DATASETS if selected_datasets is None else selected_datasets
    )
    unknown = set(selected) - PDF_SFT_DATASETS.keys()
    if unknown:
        raise ValueError(f"Unknown PDF dataset selection: {', '.join(sorted(unknown))}")

    examples: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for name in selected:
        spec = PDF_SFT_DATASETS[name]
        scan_limit = max(per_dataset_limit * 50, 5_000)
        kwargs: dict[str, Any] = {
            "split": spec["split"],
            "streaming": True,
        }
        if spec["config"]:
            kwargs["name"] = spec["config"]
        rows = load_dataset(spec["repo"], **kwargs)
        if name == "openassistant":
            scanned = []
            for row in rows:
                scanned.append(row)
                if len(scanned) >= scan_limit:
                    break
            conversations = _oasst_conversations(scanned)
        else:
            def normalized_rows() -> Iterator[list[dict[str, str]]]:
                for index, row in enumerate(rows):
                    if index >= scan_limit:
                        break
                    if not isinstance(row, dict):
                        continue
                    conversation = (
                        _wildchat_conversation(row)
                        if name == "wildchat"
                        else _row_conversation(row)
                    )
                    if conversation:
                        yield conversation

            conversations = normalized_rows()

        dataset_count = 0
        for messages in conversations:
            fingerprint = tuple(
                (message["role"], message["content"]) for message in messages
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            examples.append({"messages": messages, "source": name})
            dataset_count += 1
            if dataset_count >= per_dataset_limit:
                break
        print(f"{name}: {dataset_count} usable examples")
    return examples


def load_chat_examples(path: str) -> list[dict[str, Any]]:
    import json
    from pathlib import Path

    chat_path = Path(path).expanduser()
    if not chat_path.is_file():
        return []
    examples = []
    with chat_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            row = json.loads(line)
            messages = normalize_conversation(
                row.get("messages") if isinstance(row, dict) else None
            )
            if messages:
                examples.append({"messages": messages, "source": "opt-in-user-chat"})
            else:
                raise ValueError(f"Invalid opt-in user chat at {chat_path}:{line_number}")
    return examples
