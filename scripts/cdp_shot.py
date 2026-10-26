"""Dev helper: screenshot the floor mid-demo via the Chrome DevTools Protocol.

Usage: python scripts/cdp_shot.py <debug-port> <out.png> [sleep_seconds]
Expects Edge/Chrome already running with --remote-debugging-port.
Not part of the app or the test suite.
"""

import asyncio
import base64
import sys

import httpx
import websockets


async def main() -> None:
    port, out = int(sys.argv[1]), sys.argv[2]
    sleep = float(sys.argv[3]) if len(sys.argv) > 3 else 0
    url = sys.argv[4] if len(sys.argv) > 4 else "http://127.0.0.1:8013/"

    if sleep:
        await asyncio.sleep(sleep)

    async with httpx.AsyncClient() as client:
        targets = (await client.get(f"http://127.0.0.1:{port}/json")).json()
    host = url.split("//")[1].split("/")[0]
    page = next(t for t in targets if t["type"] == "page" and host in t["url"])

    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
        await ws.send('{"id":1,"method":"Page.captureScreenshot","params":{"format":"png"}}')
        while True:
            msg = await ws.recv()
            if '"id":1' in msg:
                import json

                data = json.loads(msg)["result"]["data"]
                with open(out, "wb") as f:
                    f.write(base64.b64decode(data))
                print(f"saved {out}")
                return


asyncio.run(main())
