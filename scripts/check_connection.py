"""Standalone check of the Beurer client, outside Home Assistant.

Verifies, against the real cloud:
  1. the OAuth password grant works,
  2. the device list comes back,
  3. the SignalR socket connects and pushes a status frame.

Read-only - it never sends a command, so your purifier is not touched.

    python scripts/check_connection.py
"""

from __future__ import annotations

import asyncio
import getpass
import json
import sys
from pathlib import Path

import aiohttp

# Import the integration's api module without needing Home Assistant installed.
sys.path.insert(0, str(Path(__file__).parent.parent))
from tests.loader import load_module

_api = load_module("api")
BeurerAuth = _api.BeurerAuth
BeurerAuthError = _api.BeurerAuthError
BeurerClient = _api.BeurerClient
BeurerConnectionError = _api.BeurerConnectionError
BeurerHub = _api.BeurerHub


async def main(email: str, password: str) -> int:
    async with aiohttp.ClientSession() as session:
        auth = BeurerAuth(session, email, password)
        client = BeurerClient(session, auth)

        print("\n[1/3] Authenticating...")
        try:
            token = await auth.async_token()
        except BeurerAuthError as err:
            print(f"  FAILED: {err}")
            return 1
        except BeurerConnectionError as err:
            print(f"  FAILED: {err}")
            return 1
        print(f"  OK - access token acquired ({len(token)} chars)")

        print("\n[2/3] Listing devices...")
        devices = await client.async_list_devices(email)
        if not devices:
            print("  FAILED: no devices on this account")
            return 1
        for d in devices:
            print(f"  OK - {d.get('model')}  id={d.get('id')}  name={d.get('name')!r}")

        print("\n[3/3] Connecting to the message hub and waiting for a status push...")
        got = asyncio.Event()
        received: dict = {}

        def on_status(status: dict) -> None:
            received.update(status)
            got.set()

        hub = BeurerHub(session, auth)
        hub.register(devices[0]["id"], on_status)
        await hub.async_start()
        try:
            await asyncio.wait_for(got.wait(), timeout=45)
        except TimeoutError:
            print("  FAILED: connected but no status frame within 45s")
            print("  (is the purifier powered on and online?)")
            await hub.async_stop()
            return 1

        print("  OK - status received:\n")
        print(json.dumps(received, indent=2))

        power = received.get("power")
        fan = received.get("fan")
        print(
            f"\n  Decoded: power={'on' if power else 'off'}  fan={fan}  "
            f"pm2.5={received.get('pm')}ug/m3  "
            f"temp={round(received.get('temperature', 0) / 256, 1)}C  "
            f"humidity={received.get('humidity')}%  "
            f"filter_replace={'YES' if received.get('filterReplace') else 'no'}"
        )

        await hub.async_stop()

    print("\nAll three checks passed.")
    return 0


if __name__ == "__main__":
    # Prompt before the event loop starts - input() blocks, and blocking inside an
    # async function stalls everything else on the loop.
    _email = input("Beurer email: ").strip()
    _password = getpass.getpass("Beurer password (not echoed, not stored): ")
    raise SystemExit(asyncio.run(main(_email, _password)))
