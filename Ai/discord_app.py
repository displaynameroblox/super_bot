"""Run the Miso Discord alpha against Super-Bot's inference providers."""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import os
import re
import secrets
import stat
import sys
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

PROVIDERS = ("openai", "gemini", "openrouter", "local", "groq")


class ConfigurationError(ValueError):
    pass


def load_private_env(path: Path, *, required: bool) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        if required:
            raise ConfigurationError("The Discord environment file is missing.") from None
        return
    except OSError:
        raise ConfigurationError("Could not open the Discord environment file.") from None
    with os.fdopen(fd, encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or (
            os.name != "nt" and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077)
        ):
            raise ConfigurationError("Use a private Discord environment file owned by you (chmod 600).")
        for name, value in dotenv_values(stream=stream, interpolate=False).items():
            if value is not None:
                os.environ.setdefault(name, value)


def selected_provider(explicit: str | None) -> str:
    provider = explicit or os.environ.get("DISCORD_PROVIDER", "").strip().lower()
    if not provider and sys.stdin.isatty():
        for number, name in enumerate(PROVIDERS, 1):
            print(f"{number}. {name}")
        choice = input("Select a provider: ").strip()
        provider = dict(zip(map(str, range(1, len(PROVIDERS) + 1)), PROVIDERS)).get(choice, choice.lower())
    if provider not in PROVIDERS:
        raise ConfigurationError("Choose openai, gemini, openrouter, local or groq with --provider or DISCORD_PROVIDER.")
    return provider


def snowflake(value: str) -> bool:
    return bool(re.fullmatch(r"[1-9][0-9]{0,19}", value)) and int(value) < 2**64


def env_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
        if minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    raise ConfigurationError(f"{name} must be an integer from {minimum} to {maximum}.")


def guild_mentions_only() -> bool:
    value = os.environ.get("DISCORD_MENTION_ONLY", "1")
    if value not in {"0", "1"}:
        raise ConfigurationError("Set DISCORD_MENTION_ONLY to 1 or 0.")
    return value == "1"


def inference_context(provider: str, managed_root: Path | None):
    os.environ["PHONE_MODEL_PROVIDER"] = "phone" if provider == "local" else provider
    os.environ["PHONE_MODEL_TIMEOUT_SECONDS"] = str(
        env_integer("DISCORD_PROVIDER_TIMEOUT", 45, 10, 180)
    )
    if provider != "local":
        return nullcontext()
    if managed_root is not None:
        from Ai import managed_model

        if managed_model.selected_model_path(managed_root) is None:
            raise ConfigurationError("The selected Super-Bot instance has no managed model.")
        return managed_model.local_model(
            managed_root, secrets.token_hex(16), quiet=True, startup_timeout=90
        )

    try:
        target = urlsplit(os.environ.get("PHONE_MODEL_BASE_URL", "").strip())
        is_loopback = bool(target.hostname) and ipaddress.ip_address(target.hostname).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback or target.username is not None:
        raise ConfigurationError(
            "For local selection, set PHONE_MODEL_BASE_URL to a loopback IP URL or use --managed-root."
        )
    return nullcontext()


def route_message(discord, bot_id, message, mention_only, channels):
    from Ai.discord_backend import Identity

    if (message.author.bot or message.webhook_id is not None
            or message.type not in {discord.MessageType.default, discord.MessageType.reply}):
        return None
    channel_id = str(message.channel.id)
    if message.guild is None:
        if not isinstance(message.channel, discord.DMChannel):
            return None
        guild_id = None
    else:
        if not isinstance(message.channel, discord.TextChannel) or channel_id not in channels:
            return None
        if mention_only and not re.search(rf"<@!?{bot_id}>", message.content):
            return None
        guild_id = str(message.guild.id)
    text = re.sub(rf"<@!?{bot_id}>", "", message.content).strip()
    if not text:
        return None
    return Identity(str(message.author.id), channel_id, guild_id), text


def run_gateway(token, backend, mention_only, *, ready_path=None):
    try:
        import discord
    except ImportError:
        raise ConfigurationError("Install the Discord extra with pip install -e '.[discord]'.") from None

    from Ai import phone_model

    intents = discord.Intents.none()
    intents.guilds = bool(backend.channels)
    intents.guild_messages = bool(backend.channels)
    intents.dm_messages = True
    intents.message_content = bool(backend.channels) and not mention_only

    class MisoClient(discord.Client):
        async def on_ready(self):
            if ready_path is not None:
                ready_path.touch()

        on_resumed = on_ready

        async def on_disconnect(self):
            if ready_path is not None:
                ready_path.unlink(missing_ok=True)

        async def on_message(self, message):
            route = route_message(discord, self.user.id, message, mention_only, backend.channels)
            # Drop excess concurrent requests instead of building a work queue.
            if route is None or busy.locked():
                return
            identity, text = route
            async with busy:
                try:
                    reply = await asyncio.to_thread(backend.reply, identity, str(message.id), text)
                except phone_model.ProviderError as exc:
                    print(str(exc), file=sys.stderr)
                    if exc.status == 429:
                        reply = "My reply service is rate limited. Please try again later."
                    elif exc.status in {400, 401, 403, 404}:
                        reply = "My reply service rejected the request. The bot owner can check its settings."
                    else:
                        reply = "My reply service is temporarily unavailable. Please try again later."
                except phone_model.PhoneModelError:
                    print("Miso's inference request failed.", file=sys.stderr)
                    reply = "I couldn't reply just now. Try again in a moment."
                except Exception:
                    print("Miso could not process a Discord message.", file=sys.stderr)
                    reply = "I couldn't reply just now. Try again in a moment."
                if reply:
                    try:
                        await message.channel.send(reply)
                    except discord.HTTPException:
                        print("Miso could not deliver a Discord message.", file=sys.stderr)

    busy = asyncio.Lock()
    client = MisoClient(intents=intents, allowed_mentions=discord.AllowedMentions.none(), max_messages=None)
    client.run(token, log_handler=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="private environment file (default: .discord.env)")
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--managed-root", type=Path, help="Super-Bot instance with a selected local model")
    parser.add_argument("--data-dir", type=Path, help="dedicated directory for Discord SQLite data")
    parser.add_argument("--profiles-file", type=Path, help="Super-Bot TUI chat_profiles.json")
    parser.add_argument("--profile-name", default="Miso")
    parser.add_argument("--import-only", action="store_true", help="refresh the owner profile without connecting")
    parser.add_argument("--copy-profile-history", action="store_true")
    parser.add_argument("--copy-knowledge", type=Path, metavar="OWNER_KNOWLEDGE_DB")
    args = parser.parse_args(argv)
    # Discord and HTTP client diagnostics may include request data.
    logging.disable(logging.CRITICAL)
    try:
        load_private_env(args.env_file or Path(".discord.env"), required=args.env_file is not None)
        owner = os.environ.get("DISCORD_OWNER_ID", "").strip()
        if not snowflake(owner):
            raise ConfigurationError("Set DISCORD_OWNER_ID to your numeric Discord user ID.")
        channels = frozenset(part.strip() for part in os.environ.get("DISCORD_CHANNEL_IDS", "").split(",")
                             if part.strip())
        if not all(map(snowflake, channels)):
            raise ConfigurationError("DISCORD_CHANNEL_IDS must contain comma-separated numeric channel IDs.")
        if args.managed_root and args.import_only:
            raise ConfigurationError("Use --managed-root with local inference, without --import-only.")
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        if not args.import_only and not token:
            raise ConfigurationError("Set DISCORD_BOT_TOKEN in your private .discord.env or environment.")

        from Ai import phone_model, tls
        from Ai.discord_backend import DiscordBackend

        mention_only = guild_mentions_only()
        node_url = os.getenv("SUPER_BOT_DISCORD_NODE_URL", "").strip()
        if node_url and not args.import_only:
            from Ai.miso_discord import NodeDiscord

            if args.provider or args.managed_root or args.copy_profile_history or args.copy_knowledge:
                raise ConfigurationError("Configure inference and shared memory on the node in node mode.")
            node_token = os.getenv("SUPER_BOT_DISCORD_NODE_TOKEN", "").strip()
            if len(node_token) < 32 or not node_token.isascii():
                raise ConfigurationError("Set the private Discord node gateway token.")
            backend = NodeDiscord(node_url, node_token, channels)
            try:
                status = backend.request("status", {}, timeout=5)
                print(f"Connected to the Miso node. {status.get('model', '')}", flush=True)
            except phone_model.PhoneModelError as exc:
                print(f"{exc}. The next message will try again.", file=sys.stderr)
            ready_path = os.environ.get("SUPER_BOT_DISCORD_READY")
            run_gateway(token, backend, mention_only, ready_path=Path(ready_path) if ready_path else None)
            return 0
        provider = None if args.import_only else selected_provider(args.provider)
        if args.managed_root and provider != "local":
            raise ConfigurationError("--managed-root requires the local provider.")
        base = tls.app_data_dir()
        memory = None
        if not args.import_only and os.getenv("SUPER_BOT_MEMORY_DB"):
            from Ai import database, miso_memory

            memory_path = Path(os.environ["SUPER_BOT_MEMORY_DB"]).expanduser()
            if not memory_path.is_file():
                raise ConfigurationError("The configured Super-Bot memory database does not exist.")
            memory = miso_memory.MemoryStore.enabled(database.Database(memory_path))
            if memory is None:
                raise ConfigurationError("Enable shared Miso memory in the configured TUI database first.")
        backend = DiscordBackend(
            (args.data_dir or base / "discord").expanduser(), owner, channels,
            env_integer("DISCORD_DAILY_REQUESTS", 60, 1, 10000),
            env_integer("DISCORD_USER_COOLDOWN", 10, 1, 3600),
            memory=memory,
        )
        if args.import_only or args.copy_profile_history or args.copy_knowledge or (memory is None and not backend.has_owner_profile()):
            try:
                backend.import_owner(
                    (args.profiles_file or base / "chat_profiles.json").expanduser(),
                    args.profile_name,
                    copy_history=args.copy_profile_history,
                    knowledge_file=args.copy_knowledge.expanduser() if args.copy_knowledge else None,
                )
            except Exception:
                raise ConfigurationError(
                    "Could not import Miso. Check the TUI profile name, profile file and optional knowledge database."
                ) from None

        if args.import_only:
            print("Imported the owner’s Miso profile.")
            return 0

        with inference_context(provider, args.managed_root.expanduser() if args.managed_root else None):
            try:
                phone_model.probe()
            except phone_model.PhoneModelError:
                raise ConfigurationError(
                    f"The {provider} readiness check failed. Check the selected model, credentials and endpoint."
                ) from None
            print(f"Starting Miso with {provider}.", flush=True)
            ready_path = os.environ.get("SUPER_BOT_DISCORD_READY")
            if ready_path:
                run_gateway(token, backend, mention_only, ready_path=Path(ready_path))
            else:
                run_gateway(token, backend, mention_only)
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130
    except Exception:
        print("Miso could not start or connect. Check Discord credentials, permissions and the data directory.",
              file=sys.stderr)
        return 1
    return 0


def managed_main(env_file):
    """A managed bot exits when its parent closes the lifetime pipe."""
    import threading

    def watch_parent():
        sys.stdin.buffer.read()
        os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()
    return main(["--env-file", env_file])


if __name__ == "__main__":
    raise SystemExit(main())
