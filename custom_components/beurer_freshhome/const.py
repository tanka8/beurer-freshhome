"""Constants for the Beurer FreshHome integration."""

DOMAIN = "beurer_freshhome"

# OAuth2. These are constants embedded in the FreshHome app itself, shared by every
# user of it - they are not per-account secrets.
SSO_BASE = "https://sso.connect.beurer.com"
API_BASE = "https://freshhome.connect.beurer.com"
TOKEN_URL = f"{SSO_BASE}/connect/token"
CLIENT_ID = "beurersso"

# Lifted from the FreshHome app. It is the same for every user of that app - it is
# not an account secret - but it IS Beurer's to change. If they ever rotate it, every
# install breaks at once, so it can be overridden per config entry without waiting
# for a new release. See CONF_CLIENT_SECRET.
DEFAULT_CLIENT_SECRET = "xm(n%TJe:~6s#5Sw"
SCOPE = "sso offline_access freshhome"
AUTH_VERSION = "2"

# REST
DEVICES_URL = f"{API_BASE}/api/users/list"
BORDER_VALUES_URL = f"{API_BASE}/api/devices/borderValues"

# SignalR
HUB_NEGOTIATE_URL = f"{API_BASE}/messageHub/negotiate"
HUB_URL = f"{API_BASE}/messageHub"

# SignalR frames are delimited by an ASCII record separator rather than newlines.
RECORD_SEPARATOR = "\x1e"
MSG_INVOCATION = 1
MSG_PING = 6

# The command envelope the app sends. The version string is a protocol marker the
# server expects, not a date to keep current.
CMD_SOURCE = "Android"
CMD_VERSION = "2018-08-31"

# Controllable functions.
FN_POWER = "power"
FN_FAN = "fan"
FN_UV = "uv"
FN_SLEEP = "sleep"
FN_MODE = "mode"

# Fan speeds per model. Only the LR-500 has been tested; anything else falls back
# to the default, which may be wrong for that model - see README.
DEFAULT_FAN_SPEEDS = [1, 2, 3, 4, 5]
MODEL_FAN_SPEEDS: dict[str, list[int]] = {
    "LR500": [1, 2, 3, 4, 5],
}

# The app labels the top speed "Turbo"; on the wire it is just the highest number.
TURBO = "turbo"


def fan_speeds(model: str) -> list[int]:
    """Speeds a model accepts, falling back to the default for unknown models."""
    return MODEL_FAN_SPEEDS.get((model or "").upper(), DEFAULT_FAN_SPEEDS)


def fan_speed_names(model: str) -> dict[int, str]:
    """Map wire speed -> label, with the highest speed labelled Turbo."""
    speeds = fan_speeds(model)
    return {s: (TURBO if s == speeds[-1] else str(s)) for s in speeds}


# airquality is reported 1-4, best to worst.
AIR_QUALITY_LABELS = {1: "good", 2: "moderate", 3: "poor", 4: "very_poor"}

# How eagerly auto mode reacts to the particle count. Written via borderValues.
# The app sends "moderate " with a trailing space - a bug on its side; the server
# stores it trimmed, so the clean value is what we send.
PM_SENSITIVITIES = ["sensitive", "standard", "moderate"]

# Optional per-entry override for DEFAULT_CLIENT_SECRET.
CONF_CLIENT_SECRET = "client_secret"


def client_secret_from_options(options) -> str:
    """The secret an entry should use.

    An override stored in the entry's options wins over the value bundled with the
    release, so a rotation by Beurer can be worked around without waiting for one.
    """
    return options.get(CONF_CLIENT_SECRET) or DEFAULT_CLIENT_SECRET


MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MODES = [MODE_AUTO, MODE_MANUAL]

# Seconds without a status push before the device is treated as unavailable. The
# server pushes roughly every 5s (it advertises updateInterval=5), so this is
# generous enough to ride out a brief reconnect.
STALE_AFTER = 120

# How long setup waits for the first status frame before assuming the default
# entity set. Long enough for a slow cloud, short enough not to stall startup.
FIRST_STATUS_TIMEOUT = 20

# Ceiling on the hub's reconnect backoff.
MAX_BACKOFF = 300
