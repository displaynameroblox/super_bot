"""Optional SBP gateway mounted on the existing Super-Bot HTTPS server."""
from pathlib import Path
import os
import json
import time
from uuid import UUID
from .host import Host
from .http import create_app, load_config
from .runtime import ExistingProvider, Runtime
from .store import Store

from Ai import database, miso_memory


def _miso_instruction():
    profile_id = os.getenv("SUPER_BOT_SBP_MISO_PROFILE", "").strip()
    path = os.getenv("SUPER_BOT_MEMORY_API_DB", "").strip()
    if not profile_id or not path:
        return None
    db = database.Database(Path(path).expanduser())

    def instruction(agent_id):
        if agent_id != "miso":
            return ""
        with db.read_connection() as conn:
            profile = conn.execute(
                "SELECT persona, style, skills_json FROM miso_profiles WHERE id=? AND user_id IS NOT NULL",
                (profile_id,),
            ).fetchone()
        if profile is None:
            return ""
        return "\n".join(filter(None, (
            profile["persona"], profile["style"], *json.loads(profile["skills_json"]).values(),
        )))[:5000]

    return instruction


class MinecraftMisoMemory:
    def __init__(self):
        self.db = database.Database(Path(os.environ["SUPER_BOT_MEMORY_API_DB"]).expanduser())
        self.profile_id = os.environ["SUPER_BOT_SBP_MISO_PROFILE"].strip()

    def context(self, text):
        with self.db.read_connection() as conn:
            return miso_memory.format_memories(miso_memory.MemoryStore._search(conn, self.profile_id, text))

    def save(self, conversation_id, request_id, text, reply):
        with self.db.connection() as conn:
            # The private SBP exchange can be replayed. Its conversation/request key
            # prevents a repeat from adding another shared memory record.
            miso_memory.MemoryStore._insert(
                conn, self.profile_id, "minecraft", f"{conversation_id}:{request_id}",
                conversation_id, text, reply, time.time(),
            )


def attach(gateway, config: Path, data_dir: Path):
    principals, limits, existing_provider = load_config(config)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt" and data_dir.stat().st_mode & 0o077:
        raise ValueError("SBP data directory must be private (chmod 700)")
    store = Store(data_dir / "sbp.sqlite3")
    try:
        host = Host(store, principals, limits)
        character = _miso_instruction()
        world = os.getenv("SUPER_BOT_SBP_MISO_MINECRAFT_WORLD", "").strip()
        player = os.getenv("SUPER_BOT_SBP_MISO_MINECRAFT_PLAYER", "").strip()
        scope = None
        if character and world and player:
            world, player = str(UUID(world)), str(UUID(player))
            scope = (f"npc-{world}", f"mc-{world}", world, player)
        memory = MinecraftMisoMemory() if scope else None
        runtime = Runtime(
            host, character_instruction=character, miso_minecraft_scope=scope, miso_minecraft_memory=memory,
        )
        if existing_provider:
            runtime.register_model("existing", ExistingProvider())
            if scope:
                runtime.register_model("miso", ExistingProvider())
        # This is opt-in; existing encrypted chat endpoints and their gateway
        # remain unchanged. SBP has its own bearer credentials and TLS boundary.
        sbp_app = create_app(host, runtime)
        sbp_app.router.routes[:] = [
            route for route in sbp_app.router.routes
            if route.path == "/.well-known/superbot" or route.path.startswith("/sbp/v0.1/")
        ]
        gateway.mount("/", sbp_app)
    except BaseException:
        store.close()
        raise
    return store
