"""Offline acceptance demo: one real HTTPS host, two mock environments, two agents.

Run `python -m Ai.universal.demo`. Credentials and data live in a temporary directory.
"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import socket
import tempfile
import threading
import time

from . import contracts as c
from .adapter import HTTPTransport, PREFIX
from .host import Host
from .http import create_app
from .mock import MockAdapter, MOVE
from .security import Principal, digest, new_token
from .store import Store


def run_demo():
    from Ai import tls
    import uvicorn
    with tempfile.TemporaryDirectory(prefix="sbp-demo-") as temp:
        root = Path(temp)
        credentials = {key: new_token() for key in ("game-sdk", "robot-sdk", "operator")}
        principals = [Principal("game-sdk", digest(credentials["game-sdk"]), "adapter", frozenset({"game"}), frozenset({MOVE.id})),
                      Principal("robot-sdk", digest(credentials["robot-sdk"]), "adapter", frozenset({"robot"}), frozenset({MOVE.id})),
                      Principal("operator", digest(credentials["operator"]), "controller", frozenset({"game", "robot"}),
                                frozenset({MOVE.id}), frozenset({"move"}))]
        host = Host(Store(root / "host.db"), principals)
        cert = tls.prepare_server_tls(directory=root / "tls")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(host), ssl_certfile=str(cert.certfile), ssl_keyfile=str(cert.keyfile),
                                               log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Demo HTTPS host failed to start")
                time.sleep(0.02)

            async def scenario():
                transport = HTTPTransport(f"https://127.0.0.1:{port}", ca_file=str(cert.certfile))
                game = MockAdapter(transport, environment_id="game", kind="game", ledger=root / "game.db")
                robot = MockAdapter(transport, environment_id="robot", kind="robotics", ledger=root / "robot.db")
                try:
                    await asyncio.gather(game.connect(credentials["game-sdk"]), robot.connect(credentials["robot-sdk"]))
                    grant = await transport.request("POST", PREFIX + "/sessions", token=credentials["operator"], body={"versions": ["0.1"]})
                    token = grant["access_token"]
                    for agent_id, env in [("explorer", "game"), ("assistant", "robot")]:
                        await transport.request("POST", PREFIX + "/agents", token=token, body=c.AgentRequest(
                            id=agent_id, name=agent_id, environments=[env], capabilities=[MOVE.id]).model_dump())
                        await transport.request("POST", f"{PREFIX}/agents/{agent_id}/actions", token=token,
                                                body=c.ActionRequest(id="move-1", environment_id=env, capability=MOVE.id, version="1", input={"x": 4}).model_dump())
                    await asyncio.gather(game.run_once(), robot.run_once())
                    before = game.session.session_id
                    await game.reconnect()
                    await asyncio.gather(game.run_once(), robot.run_once())
                    await game.observe(c.ObservationRequest(id="position-1", agent_id="explorer", kind="position", data={"x": 4}, observed_at=time.time()))
                    actions = [await transport.request("GET", f"{PREFIX}/agents/{agent}/actions/move-1", token=token)
                               for agent in ("explorer", "assistant")]
                    events = await transport.request("GET", PREFIX + "/agents/explorer/events", token=token)
                    return {"transport": "HTTPS with verified local certificate", "environments": 2, "agents": 2,
                            "actions": [a["state"] for a in actions], "reconnected_session_preserved": game.session.session_id == before,
                            "observation_received": any(e["type"] == "observation" for e in events["events"]), "cloud_services": 0}
                finally:
                    game.close()
                    robot.close()
                    await transport.close()
            return asyncio.run(scenario())
        finally:
            server.should_exit = True
            thread.join(10)
            listener.close()
            host.store.close()


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2))
