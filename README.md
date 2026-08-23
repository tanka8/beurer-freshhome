# Beurer FreshHome for Home Assistant

[![tests](https://github.com/tanka8/beurer-freshhome/actions/workflows/tests.yml/badge.svg)](https://github.com/tanka8/beurer-freshhome/actions/workflows/tests.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)

Home Assistant integration for Beurer air purifiers that use the **beurer FreshHome**
app. Reverse engineered against an **LR-500**.

> **Tested on one model.** Everything here was worked out from an LR-500 on a single
> account. Other FreshHome devices will probably work, but their fields and speed
> ranges may differ - see [Other models](#other-models).

## Why it is cloud-based

The device has **no local control path at all**. It holds one outbound MQTT connection
to AWS IoT and listens on nothing - a full port scan finds no open TCP ports. There is
no local API to talk to, so everything goes through Beurer's cloud. `tuya-local`,
LocalTuya and friends are irrelevant here: this is Beurer's own stack, not a
white-label Tuya device.

## Entities

| Entity | Type | Notes |
|---|---|---|
| Air purifier | `fan` | on/off, speeds, presets `auto` / `manual` |
| Mode | `select` | `auto` / `manual` |
| Fan speed | `select` | `1`...`turbo` - shows the speed actually running, including the one auto picked |
| Fan speed | `sensor` | the same value numerically (0 = off), for history and automations |
| Auto sensitivity | `select` | `sensitive` / `standard` / `moderate` |
| UV lamp | `switch` | |
| Night mode | `switch` | independent of auto |
| PM2.5 | `sensor` | ug/m3 |
| Air quality | `sensor` | good / moderate / poor / very poor |
| Temperature | `sensor` | degrees C |
| Humidity | `sensor` | % |
| Filter life remaining | `sensor` | hours |
| Timer | `sensor` | minutes |
| Filter | `binary_sensor` | problem class - on when a change is due |

Entities are only created for fields your device actually reports, so an untested
model gets what it supports rather than a row of broken entities.

State is **pushed** every few seconds over a SignalR WebSocket. Nothing polls.

### Why mode and fan speed are separate

The app treats them as two different things, and merging them loses information: in
auto the device still picks a numbered speed, so a combined control would only ever
read `auto` and hide what is actually running.

Setting a speed by hand leaves auto first, or the device overrides it straight back.
Night mode is deliberately left alone when changing speed - it appears to be mainly
the display-off setting, and cancelling it as a side effect would turn your display
back on.

## Install

**HACS** - three dots menu, Custom repositories, add
`https://github.com/tanka8/beurer-freshhome` as an *Integration*, then install and
restart.

**Manually** - copy `custom_components/beurer_freshhome/` into your
`config/custom_components/` and restart.

Then **Settings, Devices & services, Add integration, Beurer FreshHome**, and sign in
with your FreshHome account.

## Other models

If your device is not an LR-500, please open an
[unsupported model issue](https://github.com/tanka8/beurer-freshhome/issues/new?template=unsupported_model.yml)
and attach the diagnostics download. It contains the raw status frame with device ids
and your email redacted, which is exactly what is needed to add support.

Fan speed ranges are per model in `const.py`; unknown models fall back to 1-5, which
may be wrong for yours.

## How it works

Two channels, which is the part that is easy to get wrong:

* **REST** - `/api/users/list` returns the account's devices, identity only, **no live
  state**. `/api/devices/borderValues` holds the comfort ranges and the auto
  sensitivity, and is a full replace on write, not a patch.
* **SignalR** - `POST /messageHub/negotiate`, then a WebSocket to `/messageHub?id=...`.
  This carries both the live status pushes and the control commands. Frames are
  `0x1e`-delimited JSON; the client invokes `SendCommand`, the server pushes
  `ReceiveMessage`.

Auth is an OAuth2 password grant against `https://sso.connect.beurer.com/connect/token`.
The `client_id` and `client_secret` in `const.py` are constants embedded in the app,
identical for every FreshHome user - they are not account secrets.

### If sign-in suddenly stops working for everyone

That `client_secret` belongs to Beurer, and they can change it whenever they like.
If they do, every install fails at once - so it can be replaced **without waiting for
a new release**:

**Settings, Devices & services, Beurer FreshHome, Configure** - paste the new value
into *App client secret override*. Leave it blank to go back to the bundled one.

The integration tells the two failure modes apart rather than making you guess: OAuth2
reports a bad app secret as `invalid_client` and a bad password as `invalid_grant`, so
a rotation says the client secret was rejected instead of claiming your password is
wrong. If you hit this, please check the
[issues](https://github.com/tanka8/beurer-freshhome/issues) - someone has probably
already posted the new value.

**One WebSocket per account**, not per device: it receives every device's frames and
dispatches by device id. Both the negotiate call and the upgrade share one `aiohttp`
session on purpose, because Azure App Service sets an affinity cookie at negotiate and
the socket has to land on the same instance.

### Decoding notes

Two fields are not what they look like:

* **`pm` is tenths of a ug/m3.** Correlating 538 captured status frames against the
  device's own `airquality` band puts the good/moderate boundary exactly at `pm`
  100/101 - that is 10.0 ug/m3, the standard threshold. Taken raw, the device would be
  calling 100 ug/m3 "good".
* **`deviceLocation` is not a room.** It is the comfort-profile preset paired with the
  temperature and humidity ranges, and flips to `manual` as soon as any of them is
  customised. It is not used as the Home Assistant area.

## Tests

```bash
pip install pytest aiohttp
python -m pytest tests/
```

The suite replays real captured SignalR frames through the parser - no credentials, no
network. It covers the status decoding, per-device dispatch, that command echoes are
not mistaken for state, and that keepalive pings are answered.

`scripts/check_connection.py` is a manual end-to-end check against the real cloud. It
prompts for credentials and is read-only - it never sends a command.

## Known gaps

* **`temperature` decoding is an assumption.** Observed values were only whole
  multiples of 256, so "divide by 256" and "the high byte is the temperature" fit
  equally well. Whole degrees are right either way; fractions are unproven.
* Whether night mode caps the fan speed is unconfirmed.
* `timerMin` is read-only; setting a timer is not implemented.
* The comfort ranges (`tempMin/Max`, `humidMin/Max`) are read but not exposed, though
  the write endpoint is known.
* Schedules (`/api/deviceSchedules/*`) and history (`/api/devices/statistics`) are
  mapped but unused.

## A caveat worth stating

This talks to an undocumented API. Beurer can change or break it at any time, and
nothing here is endorsed by or affiliated with Beurer.

## Licence

MIT.
