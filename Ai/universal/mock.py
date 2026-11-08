"""Mock game and robotics adapters; intentionally no engine or hardware dependencies."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from . import contracts as c
from .adapter import Adapter, HTTPTransport, CapabilityRegistry
from .store import Store


MOVE = c.Capability(
    id="navigation.move_to", version="1", permissions=["move"], cancellation=True,
    input_schema={"type": "object", "properties": {"x": {"type": "number", "minimum": -100, "maximum": 100}},
                  "required": ["x"], "additionalProperties": False},
    output_schema={"type": "object", "properties": {"arrived": {"type": "boolean"}},
                   "required": ["arrived"], "additionalProperties": False})


class MockAdapter(Adapter):
    def __init__(self, transport, *, environment_id: str, kind: str, ledger: str | Path = ":memory:", clock=time.time):
        if kind not in {"game", "robotics"}:
            raise ValueError("Mock kind must be game or robotics")
        super().__init__(transport, c.EnvironmentRequest(id=environment_id, name=f"Mock {kind}", kind=kind, capabilities=[MOVE]))
        self.ledger, self.clock = Store(ledger), clock
        self.registry = CapabilityRegistry()
        self.registry.register(MOVE, self.move)
        self.executions = 0
        self.local_stop = False

    def move(self, input):
        # An independent adapter safety gate. Even an authorized host action cannot
        # override it. This is a mock example, NOT a physical robotics safety system.
        if self.local_stop:
            raise ValueError("Local safety stop")
        self.executions += 1
        return {"arrived": True}

    async def run_once(self):
        count = 0
        for action in await self.poll():
            if self.clock() >= action.deadline:
                continue
            key = f"{self.environment.id}/{action.agent_id}/{action.id}"
            with self.ledger.transaction():
                saved = self.ledger.get("result", key)
            if saved:
                await self.update(action, c.ActionUpdate.model_validate(saved))
                count += 1
                continue
            if action.state == "cancel_requested":
                await self.update(action, c.ActionUpdate(state="cancelled", progress=action.progress))
                count += 1
                continue
            if action.state == "queued":
                action = await self.update(action, c.ActionUpdate(state="acknowledged"))
            if action.state == "acknowledged":
                await self.update(action, c.ActionUpdate(state="running", progress=0.5))
                continue  # next poll permits cancellation and a fresh host authorization check
            try:
                result = self.registry.execute(action)
                update = c.ActionUpdate(state="completed", progress=1.0, result=result)
            except Exception:
                update = c.ActionUpdate(state="failed", progress=action.progress,
                                        error=c.Diagnostic(code="native_execution_failed", message="Native execution or local safety check failed"))
            with self.ledger.transaction():
                self.ledger.put("result", key, update.model_dump())
            await self.update(action, update)
            count += 1
        return count

    def close(self):
        self.ledger.close()


def main():
    parser = argparse.ArgumentParser(description="Run a mock SBP game or robotics adapter")
    parser.add_argument("--url", default="https://127.0.0.1:8767")
    parser.add_argument("--ca", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--kind", choices=["game", "robotics"], required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--agent", help="Register a persistent agent owned by this adapter")
    parser.add_argument("--once", action="store_true", help="Run two polling cycles and exit")
    args = parser.parse_args()
    credential = os.getenv("SBP_ADAPTER_TOKEN", "")
    if not credential:
        parser.error("Set SBP_ADAPTER_TOKEN in the process environment")

    async def run():
        transport = HTTPTransport(args.url, ca_file=args.ca)
        adapter = MockAdapter(transport, environment_id=args.environment, kind=args.kind, ledger=args.ledger)
        try:
            await adapter.connect(credential)
            if args.agent:
                await adapter.register_agent(c.AgentRequest(id=args.agent, name=args.agent, environments=[args.environment], capabilities=[MOVE.id]))
            if args.once:
                await adapter.run_once()
                await adapter.run_once()
                print(json.dumps({"environment": args.environment, "executions": adapter.executions}))
            else:
                while True:
                    try:
                        await adapter.run_once()
                    except Exception as exc:
                        from .adapter import RemoteError
                        import httpx
                        if isinstance(exc, RemoteError) and exc.status == 401:
                            await adapter.connect(credential)
                        elif not isinstance(exc, httpx.TransportError):
                            raise
                    await asyncio.sleep(1)
        finally:
            adapter.close()
            await transport.close()
    asyncio.run(run())


if __name__ == "__main__":
    main()
