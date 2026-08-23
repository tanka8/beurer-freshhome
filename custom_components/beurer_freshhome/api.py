"""Client for the Beurer FreshHome cloud.

Two channels are involved, which is the non-obvious part of this API:

  * REST, which lists the account's devices and holds the comfort/sensitivity
    settings, and
  * a SignalR WebSocket at /messageHub carrying BOTH the live status pushes and
    the control commands.

The REST device list contains no live state at all, so the WebSocket is not an
optimisation here - it is the only way to read or change anything.

The hub is per ACCOUNT, not per device: one socket receives the status frames for
every device on the account and dispatches them to whichever listeners care. An
earlier version opened one socket per device, which worked only because each
listener discarded the frames that were not its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable

import aiohttp

from .const import (
    AUTH_VERSION,
    BORDER_VALUES_URL,
    CLIENT_ID,
    CMD_SOURCE,
    CMD_VERSION,
    DEFAULT_CLIENT_SECRET,
    DEVICES_URL,
    HUB_NEGOTIATE_URL,
    HUB_URL,
    MAX_BACKOFF,
    MSG_INVOCATION,
    MSG_PING,
    RECORD_SEPARATOR,
    SCOPE,
    TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

StatusCallback = Callable[[dict], None]


class BeurerError(Exception):
    """Base class for this integration's errors."""


class BeurerAuthError(BeurerError):
    """The user's email or password was rejected."""


class BeurerClientSecretError(BeurerError):
    """The app-level client_secret was rejected.

    Distinct from BeurerAuthError on purpose. OAuth2 reports a bad client_secret as
    `invalid_client` and bad user credentials as `invalid_grant`, so the two are
    distinguishable - and telling them apart matters, because a stale client_secret
    affects every user at once and is fixed by the override, not by the user
    re-typing a password that was never wrong.
    """


class BeurerConnectionError(BeurerError):
    """The cloud could not be reached."""


def _token_error(status: int, body: str) -> BeurerError:
    """Classify a rejected token request.

    OAuth2 puts a machine-readable reason in the body. Falling back to a plain auth
    error when it is missing keeps behaviour sane against a server that does not
    follow the spec.
    """
    try:
        error = json.loads(body).get("error", "")
    except (json.JSONDecodeError, AttributeError):
        error = ""

    if error == "invalid_client":
        return BeurerClientSecretError(
            "The app client_secret was rejected. Beurer has most likely rotated it; "
            "a replacement can be set on the integration without a new release."
        )
    if error == "invalid_grant":
        return BeurerAuthError("Email or password rejected")
    return BeurerAuthError(
        f"Credentials rejected ({status}{': ' + error if error else ''})"
    )


class BeurerAuth:
    """Holds the OAuth2 tokens and refreshes them before they expire."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        client_secret: str | None = None,
    ):
        self._session = session
        self._email = email
        self._password = password
        self._client_secret = client_secret or DEFAULT_CLIENT_SECRET
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def async_token(self) -> str:
        """Return a valid access token, refreshing or re-logging in as needed."""
        async with self._lock:
            # Refresh a minute early rather than racing the expiry.
            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token

            if self._refresh_token:
                try:
                    await self._request(
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": self._refresh_token,
                        }
                    )
                    return self._access_token  # type: ignore[return-value]
                except BeurerAuthError:
                    _LOGGER.debug(
                        "Refresh token rejected, falling back to a full login"
                    )
                    self._refresh_token = None

            await self._request(
                {
                    "grant_type": "password",
                    "username": self._email,
                    "password": self._password,
                }
            )
            return self._access_token  # type: ignore[return-value]

    async def _request(self, extra: dict[str, str]) -> None:
        data = {
            "client_id": CLIENT_ID,
            "client_secret": self._client_secret,
            "scope": SCOPE,
            "version": AUTH_VERSION,
            **extra,
        }
        try:
            async with self._session.post(TOKEN_URL, data=data) as resp:
                body = await resp.text()
                if resp.status in (400, 401):
                    raise _token_error(resp.status, body)
                if resp.status != 200:
                    raise BeurerConnectionError(
                        f"Token endpoint returned {resp.status}"
                    )
                payload = json.loads(body)
        except aiohttp.ClientError as err:
            raise BeurerConnectionError(f"Cannot reach Beurer SSO: {err}") from err

        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))


class BeurerClient:
    """REST calls."""

    def __init__(self, session: aiohttp.ClientSession, auth: BeurerAuth):
        self._session = session
        self._auth = auth

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._auth.async_token()}"}

    async def async_list_devices(self, email: str) -> list[dict]:
        """Return the account's devices. Identity only - no live state."""
        try:
            async with self._session.get(
                DEVICES_URL, params={"email": email}, headers=await self._headers()
            ) as resp:
                if resp.status == 401:
                    raise BeurerAuthError("Token rejected listing devices")
                resp.raise_for_status()
                payload = await resp.json()
        except aiohttp.ClientError as err:
            raise BeurerConnectionError(f"Cannot list devices: {err}") from err

        return payload.get("devices", [])

    async def async_get_border_values(self, device_id: str) -> dict:
        """Comfort ranges plus the auto-mode particle sensitivity."""
        try:
            async with self._session.get(
                BORDER_VALUES_URL,
                params={"deviceId": device_id},
                headers=await self._headers(),
            ) as resp:
                if resp.status == 401:
                    raise BeurerAuthError("Token rejected reading borderValues")
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise BeurerConnectionError(f"Cannot read borderValues: {err}") from err

    async def async_set_border_values(self, current: dict, **changes) -> None:
        """Write borderValues.

        The endpoint replaces the whole record rather than patching it, so every
        field has to be sent back or it is lost. `current` is the last read, which
        the changes are merged into.
        """
        body = {
            "deviceId": current["deviceId"],
            "deviceLocation": current.get("deviceLocation", "manual"),
            "devicePmSensitivity": current.get("devicePmSensitivity", "standard"),
            "humidMin": current.get("humidMin"),
            "humidMax": current.get("humidMax"),
            "tempMin": current.get("tempMin"),
            "tempMax": current.get("tempMax"),
        }
        body.update(changes)

        try:
            async with self._session.post(
                BORDER_VALUES_URL, json=body, headers=await self._headers()
            ) as resp:
                if resp.status == 401:
                    raise BeurerAuthError("Token rejected writing borderValues")
                resp.raise_for_status()
        except aiohttp.ClientError as err:
            raise BeurerConnectionError(f"Cannot write borderValues: {err}") from err


class BeurerHub:
    """One SignalR connection per account.

    Runs a single long-lived task that negotiates, opens the WebSocket, performs the
    SignalR handshake and pumps frames until cancelled, reconnecting with a backoff.
    Status frames are dispatched to listeners registered per device id.
    """

    def __init__(self, session: aiohttp.ClientSession, auth: BeurerAuth):
        self._session = session
        self._auth = auth
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._listeners: dict[str, list[StatusCallback]] = {}
        # Log a dropped connection once rather than on every retry; a device that is
        # simply switched off should not fill the log at increasing intervals.
        self._reported_disconnect = False
        self._connection_listeners: list[Callable[[], None]] = []

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def register(self, device_id: str, callback: StatusCallback) -> Callable[[], None]:
        """Subscribe to one device's status frames. Returns an unsubscribe callable."""
        self._listeners.setdefault(device_id, []).append(callback)

        def _unregister() -> None:
            listeners = self._listeners.get(device_id, [])
            if callback in listeners:
                listeners.remove(callback)

        return _unregister

    def register_connection_listener(self, callback: Callable[[], None]) -> None:
        """Notified whenever the connection comes up or goes down."""
        self._connection_listeners.append(callback)

    def _notify_connection_change(self) -> None:
        for callback in self._connection_listeners:
            callback()

    async def async_start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def async_stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def async_send_command(
        self, device_id: str, function: str, value: int
    ) -> None:
        """Invoke SendCommand. The inner payload is a JSON *string*, not an object."""
        if not self.connected:
            # A reconnect may be in flight; give it a moment before giving up.
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=10)
            except TimeoutError:
                raise BeurerConnectionError("Not connected to the Beurer hub") from None

        inner = json.dumps(
            {
                "function": function,
                "source": CMD_SOURCE,
                "type": "cmd",
                "value": value,
                "valueType": "int",
                "version": CMD_VERSION,
            }
        )
        frame = {
            "type": MSG_INVOCATION,
            "target": "SendCommand",
            "arguments": [device_id, inner],
        }
        assert self._ws is not None
        await self._ws.send_str(json.dumps(frame) + RECORD_SEPARATOR)

    async def _run(self) -> None:
        backoff = 5
        while True:
            try:
                await self._connect_and_pump()
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as err:
                # A closed session means the config entry is going away; retrying
                # would spin forever against something that can never recover.
                if self._session.closed:
                    _LOGGER.debug("Beurer hub stopping: session closed")
                    return
                if self._reported_disconnect:
                    _LOGGER.debug(
                        "Beurer hub still unreachable (%s); retrying in %ss",
                        err,
                        backoff,
                    )
                else:
                    _LOGGER.warning(
                        "Beurer hub connection lost (%s); retrying in %ss", err, backoff
                    )
                    self._reported_disconnect = True
            finally:
                was_connected = self._connected.is_set()
                self._connected.clear()
                self._ws = None
                if was_connected:
                    self._notify_connection_change()

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _connect_and_pump(self) -> None:
        headers = {"Authorization": f"Bearer {await self._auth.async_token()}"}

        # Negotiate first. The response carries the connectionId, and its Set-Cookie
        # pins us to one Azure App Service instance - aiohttp's cookie jar carries
        # that onto the upgrade, which matters because the socket would otherwise be
        # opened against a different instance.
        async with self._session.post(HUB_NEGOTIATE_URL, headers=headers) as resp:
            if resp.status == 401:
                raise BeurerAuthError("Token rejected at negotiate")
            resp.raise_for_status()
            negotiation = await resp.json()

        connection_id = negotiation["connectionId"]

        async with self._session.ws_connect(
            f"{HUB_URL}?id={connection_id}", headers=headers, heartbeat=30
        ) as ws:
            self._ws = ws
            # The SignalR handshake must be the first frame.
            await ws.send_str(
                json.dumps({"protocol": "json", "version": 1}) + RECORD_SEPARATOR
            )
            self._connected.set()
            self._notify_connection_change()
            if self._reported_disconnect:
                _LOGGER.info("Beurer hub connection restored")
                self._reported_disconnect = False
            else:
                _LOGGER.debug("Connected to the Beurer message hub")

            async for msg in ws:
                if msg.type is aiohttp.WSMsgType.TEXT:
                    await self._handle_text(msg.data, ws)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

        raise BeurerConnectionError("WebSocket closed")

    async def _handle_text(
        self, data: str, ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        for chunk in filter(None, data.split(RECORD_SEPARATOR)):
            try:
                frame = json.loads(chunk)
            except json.JSONDecodeError:
                continue

            if frame.get("type") == MSG_PING:
                # SignalR's own keepalive, separate from the WebSocket-level ping
                # aiohttp handles via heartbeat.
                await ws.send_str(json.dumps({"type": MSG_PING}) + RECORD_SEPARATOR)
                continue

            if frame.get("target") != "ReceiveMessage":
                continue

            for arg in frame.get("arguments") or []:
                if not isinstance(arg, str):
                    continue
                try:
                    payload = json.loads(arg)
                except json.JSONDecodeError:
                    continue
                # The server echoes commands back on the same channel; only genuine
                # status frames are state.
                if payload.get("function") != "status":
                    continue
                for callback in self._listeners.get(payload.get("deviceId"), []):
                    callback(payload)
