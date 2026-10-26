import argparse
import json
import shlex
import sys
from pathlib import Path


MAX_CHAT_LOG_LENGTH = 10_000
MAX_PERSONA_LENGTH = 4_000
MAX_PLAYER_NAME_LENGTH = 100
DEFAULT_URL = "http://127.0.0.1:8000/start_ses"


def validate_request(chat_log: str, persona: str, player: str) -> list[str]:
    errors = []
    if len(chat_log) > MAX_CHAT_LOG_LENGTH:
        errors.append(
            f"chat log is {len(chat_log)} characters; the server limit is "
            f"{MAX_CHAT_LOG_LENGTH}"
        )
    if not persona or len(persona) > MAX_PERSONA_LENGTH:
        errors.append(
            "persona must contain between 1 and "
            f"{MAX_PERSONA_LENGTH} characters"
        )
    if not player or len(player) > MAX_PLAYER_NAME_LENGTH:
        errors.append(
            "player name must contain between 1 and "
            f"{MAX_PLAYER_NAME_LENGTH} characters"
        )
    return errors


def build_curl_command(
    chat_log: str,
    persona: str,
    player: str,
    url: str = DEFAULT_URL,
) -> str:
    payload = {
        "AI_persona": persona,
        "plr_name": player,
        "Chat_log": chat_log,
    }
    encoded_payload = json.dumps(payload, ensure_ascii=False)
    return " ".join(
        (
            "curl",
            "--fail-with-body",
            "--request",
            "POST",
            "--header",
            shlex.quote("Content-Type: application/json"),
            "--data-binary",
            shlex.quote(encoded_payload),
            shlex.quote(url),
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a chat log and print a safely quoted curl command "
            "for the /start_ses endpoint."
        )
    )
    parser.add_argument("chat_log_file", type=Path, help="UTF-8 text file with the chat log")
    parser.add_argument("--persona", required=True, help="AI persona instruction")
    parser.add_argument("--player", required=True, help="Player name")
    parser.add_argument("--url", default=DEFAULT_URL, help="Session endpoint URL")
    args = parser.parse_args()

    try:
        chat_log = args.chat_log_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"Could not read chat log file: {exc}", file=sys.stderr)
        return 1

    errors = validate_request(chat_log, args.persona, args.player)
    if errors:
        for error in errors:
            print(f"Invalid request: {error}", file=sys.stderr)
        return 1

    print(build_curl_command(chat_log, args.persona, args.player, args.url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
