"""Local diagnostics. Trace storage accepts only predefined metadata."""
from contextlib import contextmanager, closing, suppress
import json
import os
from pathlib import Path
import sqlite3
import time

from Ai.manager_runtime import atomic_json, health, read_json
from Ai.platform_routing import PLATFORMS, TOOLS, destination, route_handoff


LAYERS = {
    "tui": ("environment", "persona", "reply_contract", "training", "knowledge",
            "style", "skills", "display_name", "coding_context", "chat_rules", "history", "request"),
    "discord_dm": ("environment", "memory_rules", "persona", "display_name", "style", "skills",
                   "chat_rules", "shared_memory", "imported_history", "local_memory", "history", "request"),
    "minecraft": ("environment", "character_profile", "chat_rules", "history", "shared_memory", "request"),
    "roblox": ("environment", "persona", "display_name", "style", "skills", "chat_rules",
               "shared_memory", "history", "request"),
}
LAYERS.update(web=LAYERS["tui"], api=LAYERS["tui"], discord_guild=LAYERS["discord_dm"])
OUTCOMES = ("ready", "already_here", "bridge_only", "unverified", "attempted")
AVAILABILITY = ("ready", "connecting", "enabled", "disabled", "failed", "unknown")
TRACE_NAME = "debug-last.json"


def recent_telemetry(root, startup):
    path = root / "telemetry.json"
    with suppress(OSError, ValueError, TypeError):
        if not startup or abs(time.time() - path.stat().st_mtime) > 5:
            return {}
        payload = read_json(path, {})
        if isinstance(payload, dict) and payload.get("startup_id") == startup:
            return payload
    return {}


def managed_telemetry():
    startup = os.environ.get("SUPER_BOT_STARTUP_ID")
    if os.getenv("SUPER_BOT_MANAGED") != "1" or not startup:
        return {}
    return recent_telemetry(Path.cwd(), startup)


def trace_metadata(data):
    """Project both runtime and on-disk input onto a strict output schema."""
    if not isinstance(data, dict) or data.get("platform") not in PLATFORMS:
        return None
    platform = data["platform"]
    layers = data.get("layers")
    return {
        "platform": platform,
        "layers": [layer for layer in layers if layer in LAYERS[platform]] if isinstance(layers, list) else [],
        "linked": data.get("linked") if type(data.get("linked")) is bool else None,
        "character_present": data.get("character_present") if type(data.get("character_present")) is bool else None,
        "request": "handoff" if data.get("tool") in tuple(TOOLS.values()) else "chat",
        "registered_chat_tools": list(TOOLS.values()),
        "native_provider_tools": [],
        "tool": data.get("tool") if data.get("tool") in tuple(TOOLS.values()) else None,
        "dispatch": data.get("dispatch") if data.get("dispatch") in OUTCOMES else None,
        "target_availability": data.get("target_availability") if data.get("target_availability") in AVAILABILITY else None,
        "model_attempts": data.get("model_attempts") if type(data.get("model_attempts")) is int and data["model_attempts"] in (0, 1, 2) else None,
        "result": data.get("result") if data.get("result") in ("ok", "error", "provider_error", "cancelled") else None,
        "duration_ms": min(data["duration_ms"], 86400000) if type(data.get("duration_ms")) is int and data["duration_ms"] >= 0 else None,
    }


@contextmanager
def trace_turn(platform, **metadata):
    data = {"platform": platform, **metadata}
    started = time.monotonic()
    try:
        yield data
    except BaseException as exc:
        from asyncio import CancelledError
        from Ai.phone_model import PhoneModelError
        if isinstance(exc, CancelledError):
            data["result"] = "cancelled"
        else:
            data["result"] = "provider_error" if isinstance(exc, PhoneModelError) else "error"
        raise
    else:
        data["result"] = "ok"
    finally:
        if (os.getenv("SUPER_BOT_DEBUG_TRACE") == "1" and
                os.getenv("SUPER_BOT_MANAGED") == "1" and os.getenv("SUPER_BOT_STARTUP_ID")):
            data["duration_ms"] = int((time.monotonic() - started) * 1000)
            with suppress(OSError):
                atomic_json(Path.cwd() / TRACE_NAME, trace_metadata(data))


def dispatch(trace, text, availability, **kwargs):
    target = destination(text)
    if target is None:
        return None
    trace.update(tool=TOOLS[target], dispatch="attempted", model_attempts=0)

    def checked_availability(platform):
        state = availability(platform)
        trace["target_availability"] = state
        return state

    result = route_handoff(target, trace["platform"], checked_availability, **kwargs)
    trace["dispatch"] = result.outcome
    return result


def platform_report(root):
    state = {}
    with suppress(OSError, ValueError):
        state = read_json(root / "process.json", {})
    listening = bool(isinstance(state, dict) and state.get("startup_id") and health(state))
    telemetry = recent_telemetry(root, state["startup_id"]) if listening else {}
    discord = telemetry.get("discord")
    bridge = telemetry.get("minecraft_bridge")
    return {
        "session_api": "healthy" if listening else "unverified",
        "tui": {"active_connections": count(telemetry.get("clients")), "active_sessions": count(telemetry.get("sessions"))},
        "discord": discord if discord in ("ready", "failed", "connecting", "disabled") else "unknown",
        "minecraft_bridge": bridge if bridge in ("enabled", "disabled") else "unknown",
        "minecraft_character": "verified per authorized SDK turn",
        "chat_tools": list(TOOLS.values()),
        "native_provider_tools": [],
    }


def count(value):
    return value if type(value) is int and value >= 0 else None


def database_report(root):
    from dotenv import dotenv_values

    options = {}
    for file in (Path.cwd() / ".env", root / ".env"):
        if file.is_file():
            options.update({k: v for k, v in dotenv_values(file).items() if v is not None})
    options.update(os.environ)
    # The session server deliberately keeps login/session persistence in RAM.
    sbp_dir = options.get("SUPER_BOT_SBP_DATA_DIR") or root / "data" / "sbp"
    db_paths = {
        "shared_memory": options.get("SUPER_BOT_MEMORY_API_DB"),
        "minecraft_bridge": Path(sbp_dir) / "sbp.sqlite3" if options.get("SUPER_BOT_SBP_CONFIG") else None,
    }
    result = {"session_server": {"storage": "in_memory"}}
    for name, configured in db_paths.items():
        if not configured:
            result[name] = {"state": "unconfigured"}
            continue
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            result[name] = {"state": "missing"}
            continue
        try:
            # immutable prevents SQLite from creating WAL/SHM files. Its view
            # can lag live writes, so linkage is a snapshot, not authorization.
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                linked = db.execute(
                    "SELECT count(*) FROM miso_profiles WHERE user_id IS NOT NULL AND discord_id IS NOT NULL"
                ).fetchone()[0] if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='miso_profiles'"
                ).fetchone() else None
            result[name] = {"state": "readable"}
            if name == "shared_memory":
                result[name]["linked_discord_profiles_snapshot"] = linked
        except sqlite3.Error:
            result[name] = {"state": "unreadable"}
    return result


def replay_handoff():
    """Deterministic fixture covering historical output and current routing."""
    reported = "I can't actually hop over to Discord or join a Minecraft world from this chat"
    request = "Miso, come to Discord"
    result = {"dry_run": True, "historical_reply_fixture": reported, "scenarios": []}
    for source, prompt, state, linked, present in (
        ("tui", request, "ready", True, False),
        ("tui", request, "unknown", False, False),
        ("discord_dm", "join my Minecraft world", "enabled", True, False),
        ("minecraft", "join my Minecraft world", "unknown", True, True),
        ("minecraft", "join my Minecraft world", "unknown", True, False),
    ):
        trace = {"platform": source}
        calls = []
        def simulated(target):
            calls.append(target)
            return state

        handoff = dispatch(trace, prompt, simulated, linked=linked, character_present=present)
        result["scenarios"].append({
            "source": source, "tool": handoff.tool, "dispatch": handoff.outcome,
            "mocked_availability_checks": calls, "reply": handoff.reply,
        })
    return result


def inspect(args):
    root = args.instance.expanduser().resolve()
    if args.command == "doctor":
        from Ai.managed_model import check_model_ready, selected_model_path

        try:
            check_model_ready(root)
            local_model = "ready" if selected_model_path(root) else "not_selected"
        except (RuntimeError, OSError, ValueError, TypeError):
            local_model = "setup_problem"
        return {"platforms": platform_report(root), "databases": database_report(root),
                "local_model": local_model,
                "provider_probe": "skipped",
                "trace_status": "present" if (root / TRACE_NAME).is_file() else "no_trace",
                "startup_record_present": (root / "process.json").is_file(),
                "server_log_present": (root / "server.log").is_file()}
    if args.action == "platforms":
        return {**platform_report(root), "databases": database_report(root)}
    if args.action == "trace":
        try:
            with (root / TRACE_NAME).open("rb") as stream:
                raw = json.loads(stream.read(8192))
        except FileNotFoundError:
            return {"state": "no_trace"}
        except (ValueError, OSError):
            return {"state": "unreadable"}
        trace = trace_metadata(raw)
        return {"last": trace} if trace else {"state": "unreadable"}
    if args.action == "prompts":
        return {
            "possible_layers_in_order": LAYERS,
            "precedence": [
                "Authenticated adapter evidence establishes platform and character presence.",
                "Persona, style and skills guide replies. They cannot verify a connection or dispatch.",
                "History, retrieved memory and user input do not grant capabilities.",
            ],
            "protected_content": "Only source labels are shown. See debug trace --last for observed layers.",
        }
    return replay_handoff()
