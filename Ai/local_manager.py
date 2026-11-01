"""Local model and knowledge-library operations for Super Bot."""

from __future__ import annotations

import argparse
import html.parser
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from Ai import chat_training, knowledge


MAX_SOURCE_BYTES = 20 * 1024 * 1024


def data_directory() -> Path:
    configured = os.getenv("SUPER_BOT_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif platform.system() == "Darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "super_bot"


class _HTMLTextExtractor(html.parser.HTMLParser):
    _ignored = {"script", "style", "noscript", "nav", "footer", "header"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._ignored:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._ignored and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if not self._ignored_depth:
            self.text_parts.append(data)


def _extract_html(data: bytes) -> tuple[str, str]:
    text = data.decode("utf-8", errors="replace")
    parser = _HTMLTextExtractor()
    parser.feed(text)
    return " ".join(parser.title_parts).strip(), " ".join(parser.text_parts)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Install pypdf to index PDF documents.") from exc
    from io import BytesIO

    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _index_bytes(
    *,
    data: bytes,
    source_url: str,
    title: str,
    content_type: str,
    category: str,
    author: str,
    published_at: str,
    license_name: str,
    database: Path,
) -> int:
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"Source exceeds the {MAX_SOURCE_BYTES // (1024 * 1024)} MiB limit.")
    content_type = content_type.split(";", 1)[0].strip().lower()
    if content_type in {"application/pdf"} or source_url.lower().endswith(".pdf"):
        content = _extract_pdf(data)
    elif content_type in {"text/html", "application/xhtml+xml"} or source_url.lower().endswith(
        (".html", ".htm")
    ):
        page_title, content = _extract_html(data)
        title = title or page_title
    else:
        content = data.decode("utf-8", errors="replace")
    return knowledge.add_document(
        database,
        source_url=source_url,
        title=title or source_url,
        content=content,
        category=category,
        author=author,
        published_at=published_at,
        license=license_name,
    )


def index_file(args: argparse.Namespace) -> int:
    path = args.file.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Knowledge source is not a file: {path}")
    data = path.read_bytes()
    content_type = {
        ".pdf": "application/pdf",
        ".html": "text/html",
        ".htm": "text/html",
    }.get(path.suffix.lower(), "text/plain")
    count = _index_bytes(
        data=data,
        source_url=path.as_uri(),
        title=args.title or ("" if content_type == "text/html" else path.stem),
        content_type=content_type,
        category=args.category,
        author=args.author,
        published_at=args.published_at,
        license_name=args.license,
        database=args.database,
    )
    print(f"Indexed {count} passages from {path}")
    return count


def index_url(args: argparse.Namespace) -> int:
    url = args.url.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Source URL must be an absolute http:// or https:// URL.")
    with httpx.Client(
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=True,
        trust_env=False,
        headers={"User-Agent": "SuperBot-Knowledge-Updater/1.0"},
    ) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            declared_size = response.headers.get("content-length")
            if declared_size is not None and int(declared_size) > MAX_SOURCE_BYTES:
                raise ValueError("Source exceeds the 20 MiB limit.")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise ValueError("Source exceeds the 20 MiB limit.")
                chunks.append(chunk)
            data = b"".join(chunks)
            content_type = response.headers.get("content-type", "")
            effective_url = str(response.url)
    count = _index_bytes(
        data=data,
        source_url=effective_url,
        title=args.title,
        content_type=content_type,
        category=args.category,
        author=args.author,
        published_at=args.published_at,
        license_name=args.license,
        database=args.database,
    )
    print(f"Indexed {count} passages from {effective_url}")
    return count


def download_model(args: argparse.Namespace) -> Path:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise RuntimeError(
            "Install the Hugging Face client with `pip install huggingface_hub`."
        ) from exc
    destination = args.directory.expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    if args.filename:
        path = hf_hub_download(
            repo_id=args.repo,
            filename=args.filename,
            local_dir=destination,
        )
    else:
        from huggingface_hub import snapshot_download

        path = snapshot_download(
            repo_id=args.repo,
            local_dir=destination / args.repo.replace("/", "--"),
        )
    print(f"Downloaded model to {path}")
    return Path(path)


def train_model(args: argparse.Namespace) -> None:
    if args.iters < 1 or args.batch_size < 1 or args.max_length < 2:
        raise ValueError("Iterations and batch size must be positive; max length must be at least 2.")
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, TaskType, get_peft_model
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            DataCollatorForLanguageModeling,
            Trainer,
            TrainingArguments,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Install the local training dependencies: torch, transformers, datasets, "
            "peft, and accelerate (see Ai/local_train_superbot.ipynb)."
        ) from exc

    data_dir = args.data_dir.expanduser()
    train_path, valid_path = data_dir / "train.jsonl", data_dir / "valid.jsonl"
    if not train_path.is_file() or not valid_path.is_file():
        raise FileNotFoundError(f"{data_dir} must contain train.jsonl and valid.jsonl.")

    def read_rows(path: Path) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict) or not (
                    isinstance(row.get("messages"), list)
                    or isinstance(row.get("text"), str)
                ):
                    raise ValueError(f"{path}:{line_number} needs a messages or text field.")
                rows.append(row)
        if not rows:
            raise ValueError(f"No training examples found in {path}.")
        return rows

    train_rows, valid_rows = read_rows(train_path), read_rows(valid_path)
    device = (
        "cuda" if torch.cuda.is_available()
        else "mps" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
        else "cpu"
    )
    dtype = (
        torch.bfloat16 if device == "cuda" and torch.cuda.is_bf16_supported()
        else torch.float16 if device in {"cuda", "mps"}
        else torch.float32
    )
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    if tokenizer.pad_token is None:
        if tokenizer.eos_token is None:
            raise ValueError("The selected tokenizer needs a pad or end-of-sequence token.")
        tokenizer.pad_token = tokenizer.eos_token

    def tokenize(row: dict[str, object]) -> dict[str, list[int]]:
        if isinstance(row.get("messages"), list):
            token_ids = tokenizer.apply_chat_template(
                row["messages"], tokenize=True, add_generation_prompt=False,
                truncation=True, max_length=args.max_length,
            )
        else:
            token_ids = tokenizer(
                row["text"], truncation=True, max_length=args.max_length,
            )["input_ids"]
        if not token_ids:
            raise ValueError("An example produced no tokens.")
        return {"input_ids": token_ids}

    train_dataset = Dataset.from_list([tokenize(row) for row in train_rows])
    valid_dataset = Dataset.from_list([tokenize(row) for row in valid_rows])
    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=dtype).to(device)
    model.config.use_cache = False
    model.gradient_checkpointing_enable()
    model = get_peft_model(
        model,
        LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules=[
                "q_proj", "k_proj", "v_proj", "o_proj",
                "gate_proj", "up_proj", "down_proj",
            ],
        ),
    )
    training_args = TrainingArguments(
        output_dir=str(args.adapter_dir.expanduser()),
        max_steps=args.iters,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=8,
        learning_rate=2e-4,
        eval_strategy="steps",
        eval_steps=50,
        save_strategy="no",
        logging_steps=10,
        report_to="none",
        fp16=device == "cuda" and dtype == torch.float16,
        bf16=device == "cuda" and dtype == torch.bfloat16,
        use_mps_device=device == "mps",
        dataloader_pin_memory=device == "cuda",
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=valid_dataset,
        data_collator=DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False),
    )
    trainer.train()
    adapter_dir = args.adapter_dir.expanduser()
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    print(f"Saved PEFT adapter for {device} to {adapter_dir}")


def serve_model(args: argparse.Namespace) -> None:
    if not 1 <= args.port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    if args.backend == "llama.cpp":
        if args.model_path is None or not args.model_path.expanduser().is_file():
            raise FileNotFoundError("llama.cpp serving requires an existing GGUF --model-path.")
        command = [
            args.executable,
            "--model",
            str(args.model_path.expanduser()),
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--ctx-size",
            str(args.context_size),
            "--threads",
            str(args.threads),
            "--api-key",
            args.api_key,
            "--alias",
            "superbot-phone",
        ]
    else:
        if not args.model:
            raise ValueError("MLX serving requires --model.")
        command = [
            sys.executable,
            "-m",
            "mlx_lm.server",
            "--model",
            args.model,
            "--host",
            args.host,
            "--port",
            str(args.port),
        ]
        if args.adapter_dir:
            adapter = args.adapter_dir.expanduser()
            if not adapter.is_dir():
                raise FileNotFoundError(f"Adapter directory does not exist: {adapter}")
            command.extend(["--adapter-path", str(adapter)])
    subprocess.run(command, check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("download", help="Download one model file from Hugging Face")
    download.add_argument("--repo", required=True, help="Hugging Face repository ID")
    download.add_argument(
        "--filename",
        help="Exact repository filename; omit to download the whole repository (e.g. a trained adapter)",
    )
    download.add_argument("--directory", type=Path, default=data_directory() / "models")
    download.set_defaults(handler=download_model)

    train = commands.add_parser("train", help="Fine-tune a local model with Transformers/PEFT LoRA")
    train.add_argument("--model", default="google/gemma-2-2b-it")
    train.add_argument("--data-dir", type=Path, required=True)
    train.add_argument("--adapter-dir", type=Path, default=data_directory() / "adapters" / "superbot")
    train.add_argument("--iters", type=int, default=200)
    train.add_argument("--batch-size", type=int, default=1)
    train.add_argument("--max-length", type=int, default=512)
    train.set_defaults(handler=train_model)

    export = commands.add_parser(
        "export-chats",
        help="Export explicitly opted-in chat exchanges for notebook training",
    )
    export.add_argument("--database", type=Path, default=chat_training.database_path())
    export.add_argument(
        "--output",
        type=Path,
        default=data_directory() / "training-data" / "user_chats.jsonl",
    )
    export.set_defaults(
        handler=lambda args: print(
            f"Exported {chat_training.export_exchanges(args.database, args.output)} "
            f"chat exchanges to {args.output.expanduser()}"
        )
    )

    serve = commands.add_parser("serve", help="Run a local OpenAI-compatible inference server")
    serve.add_argument("--backend", choices=("llama.cpp", "mlx"), default="llama.cpp")
    serve.add_argument("--model-path", type=Path)
    serve.add_argument("--model", help="Hugging Face model ID (MLX backend)")
    serve.add_argument("--adapter-dir", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--context-size", type=int, default=4096)
    serve.add_argument("--threads", type=int, default=max(1, os.cpu_count() or 1))
    serve.add_argument("--api-key", default=os.getenv("TEST_PHONE_API_KEY", "local-superbot"))
    serve.add_argument("--executable", default="llama-server")
    serve.set_defaults(handler=serve_model)

    index = commands.add_parser("index", help="Add or refresh one local knowledge source")
    source = index.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--url")
    index.add_argument("--title", default="")
    index.add_argument("--category", required=True)
    index.add_argument("--author", default="")
    index.add_argument("--published-at", default="")
    index.add_argument("--license", default="")
    index.add_argument("--database", type=Path, default=knowledge.default_database_path())
    index.set_defaults(handler=lambda args: index_file(args) if args.file else index_url(args))

    create = commands.add_parser("create-backup", help="Back up the accounts and chats database")
    create.add_argument("--database", type=Path)
    create.set_defaults(handler=create_database_backup)

    listing = commands.add_parser("list-backups", help="List database backups")
    listing.add_argument("--database", type=Path)
    listing.set_defaults(handler=list_database_backups)

    restore = commands.add_parser(
        "restore-backup", help="Restore the database from a backup (stop the server first)"
    )
    restore.add_argument("backup", type=Path)
    restore.add_argument("--database", type=Path)
    restore.set_defaults(handler=restore_database_backup)

    return parser


def create_database_backup(args: argparse.Namespace) -> None:
    from Ai import backup, database

    print(backup.create_backup(args.database or database.default_path()))


def list_database_backups(args: argparse.Namespace) -> None:
    from Ai import backup, database

    for info in backup.list_backups(args.database or database.default_path()):
        print(info.path)


def restore_database_backup(args: argparse.Namespace) -> None:
    from Ai import backup, database

    previous = backup.restore_backup(args.backup, args.database or database.default_path())
    print(f"Restored. Your previous data was saved to {previous}" if previous else "Restored.")


def main() -> None:
    args = build_parser().parse_args()
    handler = args.handler
    handler(args)


if __name__ == "__main__":
    main()
