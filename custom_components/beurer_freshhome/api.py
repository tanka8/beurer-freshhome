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
    INITIAL_BACKOFF,
    MAX_BACKOFF,
    MSG_INVOCATION,
    MSG_PING,
    RECORD_SEPARATOR,
    SCOPE,
    STABLE_CONNECTION,
    TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

StatusCallback = Callable[[dict], None]


class BeurerError(Exception):
    """Base class for this integration's errors."""


class BeurerAuthError(BeurerError):
    """The user's email and password, or the sign-in saved from them, was rejected."""


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
    """Holds the OAuth2 tokens and refreshes them before they expire.

    Two ways to sign in, tried in this order:

      * the refresh token, which is what a config entry keeps once Beurer has
        confirmed it works, and
      * the password, which entries created before refresh tokens were stored still
        hold until that has been confirmed (see async_confirm_refresh_token).

    Beurer may rotate the refresh token every time it is used, so a new one is handed
    to `on_refresh_token` the moment it arrives. Whoever stores it has to do so
    straight away: a rotated token that is only held in memory is lost on restart,
    and the stored one it replaced may no longer be accepted.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str | None = None,
        client_secret: str | None = None,
        *,
        refresh_token: str | None = None,
        on_refresh_token: Callable[[str], None] | None = None,
    ):
        self._session = session
        self._email = email
        self._password = password
        self._client_secret = client_secret or DEFAULT_CLIENT_SECRET
        self._access_token: str | None = None
        self._refresh_token: str | None = refresh_token
        self._on_refresh_token = on_refresh_token
        # Whether Beurer has accepted a refresh_token grant from this instance. A
        # refresh token that was merely issued is not evidence that it works.
        self._refresh_proven = False
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str | None:
        """The current refresh token, if Beurer has issued one."""
        return self._refresh_token

    async def async_token(self) -> str:
        """Return a valid access token, refreshing or re-logging in as needed."""
        async with self._lock:
            # Refresh a minute early rather than racing the expiry.
            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token

            if self._refresh_token:
                try:
                    await self._refresh()
                    return self._access_token  # type: ignore[return-value]
                except BeurerAuthError as err:
                    # Only a rejection lands here. A failure to reach Beurer is a
                    # BeurerConnectionError and leaves the token alone: it is still
                    # good, and throwing it away would cost the user a reauth for
                    # an outage.
                    self._refresh_token = None
                    if not self._password:
                        raise BeurerAuthError(
                            "Beurer rejected the saved sign-in; it has to be "
                            "entered again"
                        ) from err
                    _LOGGER.debug(
                        "Refresh token rejected, falling back to a full login"
                    )

            if not self._password:
                raise BeurerAuthError("No password or refresh token to sign in with")

            await self._request(
                {
                    "grant_type": "password",
                    "username": self._email,
                    "password": self._password,
                }
            )
            return self._access_token  # type: ignore[return-value]

    async def async_confirm_refresh_token(self) -> bool:
        """Spend the refresh token once, to learn that Beurer really accepts it.

        Storing the refresh token in place of the password is only safe if the
        token works: if Beurer issued one that it then refuses to honour, the user
        would be sent to reauth every time the access token expires. So the swap is
        made only after a refresh_token grant has actually succeeded, and the answer
        is given here without waiting an hour for it to happen on its own.

        Returns False when there is no token or Beurer rejects it, leaving the
        caller to keep the password. A failure to reach Beurer, or a rejected
        client_secret, propagates: that is "could not tell", not "no".
        """
        async with self._lock:
            if self._refresh_proven:
                return True
            if not self._refresh_token:
                return False
            try:
                await self._refresh()
            except BeurerAuthError:
                self._refresh_token = None
                return False
            return True

    async def _refresh(self) -> None:
        """Run a refresh_token grant with the token held. Caller holds the lock."""
        await self._request(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token or "",
            }
        )
        self._refresh_proven = True

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
        except ValueError as err:  # JSONDecodeError is a ValueError
            # A 200 that is not the token document. Letting a JSONDecodeError escape
            # would bypass every `except BeurerError` in the integration and surface
            # as an unhandled traceback rather than a readable message.
            raise BeurerConnectionError(
                "Token endpoint returned a response that is not valid JSON"
            ) from err

        try:
            self._access_token = payload["access_token"]
        except (KeyError, TypeError) as err:
            raise BeurerConnectionError(
                "Token endpoint returned no access_token"
            ) from err
        try:
            expires_in = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._expires_at = time.time() + expires_in

        # A response without a refresh_token keeps the one already held, and so
        # does a null or empty one.
        refresh_token = payload.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token:
            changed = refresh_token != self._refresh_token
            self._refresh_token = refresh_token
            if changed:
                self._notify_refresh_token(refresh_token)

    def _notify_refresh_token(self, refresh_token: str) -> None:
        if self._on_refresh_token is None:
            return
        try:
            self._on_refresh_token(refresh_token)
        except Exception:
            # The sign-in itself worked, so it must not be reported as failed; the
            # cost of not saving is a reauth after the next restart, not an error
            # now. Logged without the token.
            _LOGGER.exception("Could not save the new Beurer refresh token")


class BeurerClient:
    """REST calls."""

    def __init__(self, session: aiohttp.ClientSession, auth: BeurerAuth):
        self._session = session
        self._auth = auth
        # Serialises the read-modify-write of borderValues; see
        # async_set_border_values.
        self._border_values_lock = asyncio.Lock()

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

    async def async_set_border_values(self, device_id: str, **changes) -> None:
        """Change some borderValues fields, leaving the others as they are NOW.

        The endpoint replaces the whole record rather than patching it, so every
        field has to be sent back or it is lost. The record is therefore read again
        here, immediately before the write, and the changes are merged into that.

        Merging into an earlier read - the coordinator's, say, which is only as
        fresh as the last time something asked - would silently put back the old
        value of every field that changed in the meantime, for instance a comfort
        range edited in the Beurer app. Costing one extra GET per write is the
        price of not reverting somebody's setting.
        """
        # Without this, two writes could both read the record before either wrote
        # it, and the second would revert the first.
        async with self._border_values_lock:
            current = await self.async_get_border_values(device_id)
            if not isinstance(current, dict) or not current:
                # A write built from nothing would blank every other field.
                raise BeurerConnectionError(
                    "borderValues came back empty; refusing to overwrite settings "
                    "that could not be read"
                )

            body = {
                "deviceId": current.get("deviceId") or device_id,
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
                raise BeurerConnectionError(
                    f"Cannot write borderValues: {err}"
                ) from err


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
        # When the current socket came up, so a drop can be told apart from an
        # attempt that never connected. Monotonic: this measures elapsed time, and
        # a clock adjustment must not make a young connection look established.
        self._connected_at: float | None = None
        self._listeners: dict[str, list[StatusCallback]] = {}
        # Log a dropped connection once rather than on every retry; a device that is
        # simply switched off should not fill the log at increasing intervals.
        self._reported_disconnect = False
        self._connection_listeners: list[Callable[[], None]] = []
        # Set by the config entry so a credential failure at runtime can start a
        # reauth flow instead of retrying against a password that will never work.
        self.on_auth_error: Callable[[], None] | None = None
        # Set by the config entry so a client_secret rotation at runtime reloads the
        # entry, where setup reports it with the message that names the fix.
        self.on_client_secret_error: Callable[[], None] | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def _held_connection(self, seconds: float) -> bool:
        """Whether the attempt that just ended kept a socket up for this long."""
        if self._connected_at is None:
            return False
        return (time.monotonic() - self._connected_at) >= seconds

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
        ws = self._ws
        if ws is None or ws.closed:
            raise BeurerConnectionError("Not connected to the Beurer hub")

        try:
            await ws.send_str(json.dumps(frame) + RECORD_SEPARATOR)
        except (aiohttp.ClientError, OSError) as err:
            # The socket can close between the check above and the write. aiohttp
            # raises ConnectionResetError there, which is not a BeurerError, so it
            # would bypass the coordinator's translation into HomeAssistantError and
            # reach the user as a traceback.
            raise BeurerConnectionError(
                f"Could not send to the Beurer hub: {err}"
            ) from err

    async def _run(self) -> None:
        backoff = INITIAL_BACKOFF
        while True:
            try:
                await self._connect_and_pump()
            except asyncio.CancelledError:
                raise
            except BeurerClientSecretError as err:
                # Beurer rotated the app secret. Retrying cannot fix that, and it is
                # not the user's password, so reauth is the wrong prompt. Reloading
                # the entry puts the failure through setup, which reports it with the
                # message that points at the override. Without this the hub would
                # retry silently forever and the user would only ever see repeated
                # "connection lost" lines in the log.
                _LOGGER.error("Beurer app client secret rejected: %s", err)
                if self.on_client_secret_error:
                    self.on_client_secret_error()
                return
            except BeurerAuthError as err:
                # Credentials no longer work. Retrying cannot fix that, so hand it to
                # Home Assistant, which prompts the user to re-authenticate, and stop.
                _LOGGER.warning("Beurer credentials rejected: %s", err)
                if self.on_auth_error:
                    self.on_auth_error()
                return
            except Exception as err:
                # A closed session means the config entry is going away; retrying
                # would spin forever against something that can never recover.
                if self._session.closed:
                    _LOGGER.debug("Beurer hub stopping: session closed")
                    return
                # A socket that stayed up is evidence the endpoint works, so the
                # next attempt starts from the bottom of the backoff again; only
                # attempts that never produced a usable connection escalate.
                # This is checked here rather than after the loop body because
                # _connect_and_pump always raises - it ends in an unconditional
                # `raise BeurerConnectionError`, so a reset placed after the await
                # is unreachable and every drop doubled the delay until it stuck
                # at MAX_BACKOFF, leaving a healthy network reconnecting once every
                # five minutes until the entry was reloaded.
                if self._held_connection(STABLE_CONNECTION):
                    backoff = INITIAL_BACKOFF
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
                self._connected_at = None
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
            self._connected_at = time.monotonic()
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
