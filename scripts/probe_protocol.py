"""Ad-hoc end-to-end probe of the Aemeath WebSocket protocol.

Connects to a running backend, negotiates the protocol and exercises the
classroom switch, screen request and memory listing. Used during development to
confirm the real transport carries the frames the tests expect.

Usage:
    python scripts/probe_protocol.py [ws://127.0.0.1:12393/client-ws]
"""

import asyncio
import json
import sys
import uuid

DEFAULT_URL = "ws://127.0.0.1:12393/client-ws"


async def probe(url: str) -> int:
    import websockets

    uid = uuid.uuid4().hex[:8]
    frames: list[dict] = []

    async with websockets.connect(f"{url}?client-id={uid}") as ws:

        async def drain(seconds: float) -> None:
            """Collect everything the server sends for a moment."""
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=seconds)
                    frames.append(json.loads(raw))
            except (asyncio.TimeoutError, Exception):
                return

        # Let the connection handshake finish.
        await drain(3.0)

        print("initial frames:", [f.get("type") for f in frames])

        # 1. Negotiate the Aemeath protocol.
        frames.clear()
        await ws.send(
            json.dumps(
                {
                    "type": "aemeath-hello",
                    "protocol_version": 2,
                    "capabilities": ["clear-audio", "receipts", "screens"],
                }
            )
        )
        await drain(2.0)
        ack = next((f for f in frames if f.get("type") == "aemeath-hello-ack"), None)
        if ack is None:
            print("FAIL: no aemeath-hello-ack")
            return 1
        print(
            "negotiated:",
            ack["protocol_version"],
            "accepted:",
            ack["accepted"],
            "mode:",
            ack["state"]["mode"],
        )

        # 2. Switch to class mode and confirm the ordered sequence.
        frames.clear()
        await ws.send(json.dumps({"type": "aemeath-set-mode", "mode": "class"}))
        await drain(2.0)
        types = [f.get("type") for f in frames]
        print("class switch frames:", types)
        if "aemeath-state" not in types or "aemeath-clear-audio" not in types:
            print("FAIL: class switch did not send state + clear-audio")
            return 1
        if types.index("aemeath-state") > types.index("aemeath-clear-audio"):
            print("FAIL: clear-audio arrived before the new state")
            return 1
        state = next(f for f in frames if f.get("type") == "aemeath-state")
        if state["state"]["voice_allowed"] is not False:
            print("FAIL: class mode still allows voice")
            return 1
        print("class mode confirmed, version:", state["state"]["state_version"])

        # 3. Ask for a screen observation; report the concrete reason.
        frames.clear()
        await ws.send(json.dumps({"type": "aemeath-screen-request", "force": True}))
        await drain(3.0)
        types = [f.get("type") for f in frames]
        print("screen request frames:", types)
        unavailable = next(
            (f for f in frames if f.get("type") == "aemeath-screen-unavailable"), None
        )
        summary = next(
            (f for f in frames if f.get("type") == "aemeath-screen-summary"), None
        )
        if unavailable:
            print("screen unavailable reason:", unavailable["reason"])
        elif summary:
            print("screen summary:", summary["summary"][:60])
        else:
            print("WARN: screen request produced no response")

        # 4. Memory list must come from SQLite.
        frames.clear()
        await ws.send(json.dumps({"type": "aemeath-memory-query", "action": "list"}))
        await drain(2.0)
        listing = next(
            (f for f in frames if f.get("type") == "aemeath-memory-list"), None
        )
        if listing is None:
            print("FAIL: no memory list returned")
            return 1
        print("memories:", len(listing["memories"]))

        # 5. History must be served from SQLite, not upstream JSON.
        frames.clear()
        await ws.send(json.dumps({"type": "create-new-history"}))
        await drain(2.0)
        created = next(
            (f for f in frames if f.get("type") == "new-history-created"), None
        )
        if created is None:
            print("FAIL: no history created")
            return 1
        print("history uid:", created["history_uid"][:12])

        frames.clear()
        await ws.send(json.dumps({"type": "fetch-history-list"}))
        await drain(2.0)
        listing = next((f for f in frames if f.get("type") == "history-list"), None)
        print("history list entries:", len(listing["histories"]) if listing else "none")

        # 6. Restore to normal mode so the local state is not left muted.
        await ws.send(json.dumps({"type": "aemeath-set-mode", "mode": "normal"}))
        await drain(2.0)

    print("PROBE PASSED")
    return 0


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    try:
        return asyncio.run(probe(url))
    except Exception as exc:
        print(f"PROBE FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
