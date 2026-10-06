"""Client for the Gemstone Hub2 controller's local HTTP API.

This module deliberately imports nothing from Home Assistant. It is the protocol
layer -- RGBW encoding, shadow-document construction, response parsing, and an
aiohttp client. Keeping it HA-free means it unit-tests under plain pytest, and
that promoting it to a standalone PyPI package (which Home Assistant core
requires of integration API clients) stays a move rather than a rewrite.

Wire format of the Hub2 local HTTP API:

* Requests carry ``state.desired``; responses carry ``state.reported``. The
  firmware reuses its MQTT device-shadow parser for HTTP, which is why a LAN
  endpoint speaks AWS IoT shadow documents.
* Colours are 32-bit RGBW integers laid out as
  ``(warm << 24) + (blue << 16) + (green << 8) + red``. This is *not* the usual
  hex ordering: pure red is 255, not 16711680.
* ``POST /device-control/play`` returns reported state, so a command response
  doubles as a state read. Unverified: whether that reply reflects state the
  controller has *applied* or merely *accepted* (the HTTP server and the pixel
  driver are likely separate tasks). Until hardware testing settles it, callers
  should treat the reply as the best available state and let the next poll
  reconcile, rather than build on the reply being authoritative.

The document warns that it is a work in progress and endpoints are subject to
change, so parsing is written to tolerate missing and unfamiliar fields rather
than to assert a fixed shape.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final, Literal
from uuid import uuid4

import aiohttp

from .animations import CATALOG, Animation

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT: Final = 80
DEFAULT_TIMEOUT: Final = 10.0

# Sent as ``state.desired.origin``. Firmware confirmed (docs/design.md, section
# 2) that any value is accepted and echoed back in the reply; without one,
# replies say "http". So this keeps Home Assistant traffic attributable in the
# controller's logs, and a reply carrying it is provably the processing of our
# own request rather than a stale read.
DEFAULT_ORIGIN: Final = "homeassistant"

# The API documents 503 as "system busy or under maintenance". A single short
# retry covers a poll colliding with a command; anything longer belongs to the
# coordinator's own retry cycle.
BUSY_RETRY_DELAY: Final = 0.5

# Bounds from the document's property specification table.
MAX_COLORS: Final = 20  # colours per pattern
MAX_NAME_LENGTH: Final = 32  # pattern and architectural ``name``

# Every scene key a play command carries. Exactly one of ``colorB``, ``pattern``
# or ``architectural`` is set per command and the rest are explicitly null.
_SCENE_KEYS: Final = ("color", "colorB", "pattern", "architectural", "impulse", "playlist")

PATH_HUB_SETTINGS: Final = "/device-state/hub-settings"
PATH_CURRENTLY_PLAYING: Final = "/device-state/currently-playing"
PATH_PLAY: Final = "/device-control/play"

# Straight from the document's "Server Error Codes" table. Mapping them gives
# actionable log lines instead of a bare status number.
ERROR_MESSAGES: Final[dict[int, str]] = {
    400: "Malformed request, invalid JSON, or missing body",
    401: "Unauthorized (access control is a future expansion of the API)",
    404: "Invalid route",
    405: "Invalid HTTP method -- only GET and POST are supported",
    411: "Missing Content-Length header",
    413: "Request entity too large",
    415: "Invalid Content-Type -- only application/json is supported",
    500: "Internal server error",
    503: "Service temporarily unavailable -- system busy",
    505: "Invalid HTTP version -- only HTTP/1.1 is supported",
}

RGBW = tuple[int, int, int, int]

# Which of the mutually exclusive scene shapes the controller reported.
# ``playlist`` and ``impulse`` appear in every payload but the document never
# defines them beyond an orphan ``lengthInSeconds``; they are recognised so a
# controller reporting one is not mistaken for a power-only reply.
Scene = Literal["color", "pattern", "architectural", "playlist", "impulse"]


class GemstoneError(Exception):
    """Base class for every failure talking to a controller."""


class GemstoneConnectionError(GemstoneError):
    """The controller could not be reached, or did not answer in time."""


class GemstoneBusyError(GemstoneError):
    """The controller reported 503. Retryable."""


class GemstoneResponseError(GemstoneError):
    """The controller answered with an unexpected HTTP status."""

    def __init__(self, status: int) -> None:
        """Build the error from a status code, naming it where the API does."""
        self.status = status
        detail = ERROR_MESSAGES.get(status, "Unexpected response")
        super().__init__(f"HTTP {status}: {detail}")


class GemstoneParseError(GemstoneError):
    """The controller answered with a body we could not interpret."""


def parse_rgbw_int(rgbw_int: int) -> RGBW:
    """Split a packed colour integer into ``(red, green, blue, warm)``."""
    w = (rgbw_int >> 24) & 0xFF
    b = (rgbw_int >> 16) & 0xFF
    g = (rgbw_int >> 8) & 0xFF
    r = rgbw_int & 0xFF
    return r, g, b, w


def rgbw_to_int(r: int, g: int, b: int, w: int) -> int:
    """Pack four 8-bit channels into the controller's colour integer."""
    return (w << 24) + (b << 16) + (g << 8) + r


def get_brightness_from_rgbw_int(rgbw_int: int) -> int:
    """Return the brightness implied by a packed colour: its largest channel."""
    return max(parse_rgbw_int(rgbw_int))


def full_scale_rgbw(rgbw_int: int) -> RGBW:
    """Scale a brightness-baked colour integer back up to full-scale channels.

    Legacy ``color`` integers fold brightness into the channel values, whereas
    Home Assistant models hue and brightness as separate attributes. Reporting
    the raw channels alongside a brightness would dim the colour twice -- a
    light at 10% would show as near-black *and* 10%. Newer ``colorB`` payloads
    already separate the two and skip this entirely.
    """
    r, g, b, w = parse_rgbw_int(rgbw_int)
    brightness = max(r, g, b, w)
    if brightness in (0, 255):
        return r, g, b, w
    scale = 255 / brightness
    return (
        min(255, round(r * scale)),
        min(255, round(g * scale)),
        min(255, round(b * scale)),
        min(255, round(w * scale)),
    )


def clamp_byte(value: float) -> int:
    """Clamp a value into the 0-255 range the controller accepts."""
    return int(max(0, min(255, round(value))))


def clamp_speed(value: float) -> int:
    """Clamp a speed into 1-255.

    Separate from ``clamp_byte`` because brightness legitimately goes to 0 and
    speed does not: 0 is outside what the animations accept.
    """
    return int(max(1, min(255, round(value))))


@dataclass(frozen=True)
class HubSettings:
    """The subset of ``hub-settings`` this integration acts on."""

    pixel_count: tuple[int, ...]
    firmware: str | None
    firmware_spi: str | None
    firmware_wifi: str | None
    bluetooth_name: str | None
    local_ip: str | None
    tcp_enabled: bool | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class CurrentlyPlaying:
    """What the controller reports it is doing, normalised for Home Assistant.

    ``rgbw`` is always a full-scale hue with ``brightness`` carried separately,
    regardless of which colour shape the firmware sent. ``scene`` says which
    shape that was; it is ``None`` for a power-only reply, which carries no
    scene at all (API section 3.4). ``length_seconds`` is the one field the
    document gives a playlist or impulse.
    """

    on_state: bool
    rgbw: RGBW | None
    brightness: int | None
    animation: str | None
    scene: Scene | None
    length_seconds: int | None
    pattern: dict[str, Any] | None
    architectural: dict[str, Any] | None
    raw: dict[str, Any]


def _reported(payload: Any) -> dict[str, Any]:
    """Pull ``state.reported`` out of a shadow document.

    Play responses carry both ``desired`` and ``reported``; ``reported`` is
    always the authoritative half. Typed as ``Any`` because this parses network
    JSON -- the isinstance guards are the point, not redundant.
    """
    if not isinstance(payload, Mapping):
        raise GemstoneParseError(f"Expected a JSON object, got {type(payload).__name__}")
    state = payload.get("state")
    if not isinstance(state, Mapping):
        raise GemstoneParseError("Response has no 'state' object")
    reported = state.get("reported")
    if not isinstance(reported, Mapping):
        raise GemstoneParseError("Response has no 'state.reported' object")
    return dict(reported)


def parse_hub_settings(payload: Mapping[str, Any]) -> HubSettings:
    """Parse a ``GET /device-state/hub-settings`` response."""
    settings = _reported(payload).get("hubSettings")
    if not isinstance(settings, Mapping):
        raise GemstoneParseError("Response has no 'hubSettings' object")

    raw_pixel_count = settings.get("pixelCount")
    pixel_count: tuple[int, ...] = ()
    if isinstance(raw_pixel_count, Sequence) and not isinstance(raw_pixel_count, (str, bytes)):
        pixel_count = tuple(int(count) for count in raw_pixel_count if isinstance(count, (int, float)))

    return HubSettings(
        pixel_count=pixel_count,
        firmware=settings.get("firmware"),
        firmware_spi=settings.get("firmwareSpi"),
        firmware_wifi=settings.get("firmwareWifi"),
        bluetooth_name=settings.get("bluetoothName"),
        local_ip=settings.get("localIp"),
        tcp_enabled=settings.get("tcpEnabled"),
        raw=dict(settings),
    )


def parse_currently_playing(payload: Mapping[str, Any]) -> CurrentlyPlaying:
    """Parse a currently-playing or play response into normalised state.

    Handles both colour shapes: ``colorB: {value, brightness}``, which keeps hue
    and brightness apart, and the older ``color: <int>``, which bakes them
    together. A turn-on/turn-off response carries only ``onState``, so every
    other field is optional.
    """
    playing = _reported(payload).get("currentlyPlaying")
    if playing is None:
        playing = {}
    if not isinstance(playing, Mapping):
        raise GemstoneParseError("'currentlyPlaying' is not an object")

    pattern = playing.get("pattern") if isinstance(playing.get("pattern"), Mapping) else None
    architectural = playing.get("architectural") if isinstance(playing.get("architectural"), Mapping) else None
    playlist = playing.get("playlist") if isinstance(playing.get("playlist"), Mapping) else None
    impulse = playing.get("impulse") if isinstance(playing.get("impulse"), Mapping) else None
    color_b = playing.get("colorB") if isinstance(playing.get("colorB"), Mapping) else None
    legacy_color = playing.get("color")

    rgbw: RGBW | None = None
    brightness: int | None = None
    animation: str | None = None
    scene: Scene | None = None
    length_seconds: int | None = None

    if pattern is not None:
        scene = "pattern"
        animation = pattern.get("animation")
        raw_brightness = pattern.get("brightness")
        if isinstance(raw_brightness, (int, float)):
            brightness = clamp_byte(raw_brightness)
        # Surface the pattern's first colour so the frontend card has something
        # to show; HA has no concept of a multi-colour light.
        colors = pattern.get("colors")
        if isinstance(colors, Sequence) and not isinstance(colors, (str, bytes)) and colors:
            first = colors[0]
            if isinstance(first, (int, float)):
                rgbw = full_scale_rgbw(int(first))
    elif architectural is not None:
        scene = "architectural"
        raw_brightness = architectural.get("brightness")
        if isinstance(raw_brightness, (int, float)):
            brightness = clamp_byte(raw_brightness)
        # First segment's colour, for the same reason as the pattern branch.
        # Brightness is a separate field here (as with ``colorB``), so the
        # colour is a hue as-is and is not rescaled.
        static_colors = architectural.get("staticColors")
        if isinstance(static_colors, Sequence) and not isinstance(static_colors, (str, bytes)) and static_colors:
            first_segment = static_colors[0]
            if isinstance(first_segment, Mapping) and isinstance(first_segment.get("color"), (int, float)):
                rgbw = parse_rgbw_int(int(first_segment["color"]))
    elif playlist is not None or impulse is not None:
        # Opaque: the document names them and bounds lengthInSeconds, nothing
        # more. Recognised as scenes so the light is not left looking like a
        # power-only reply; no colour or brightness can be derived.
        opaque = playlist if playlist is not None else impulse
        scene = "playlist" if playlist is not None else "impulse"
        raw_length = opaque.get("lengthInSeconds")
        if isinstance(raw_length, int) and not isinstance(raw_length, bool):
            length_seconds = raw_length
    elif color_b is not None and isinstance(color_b.get("value"), (int, float)):
        scene = "color"
        rgbw = parse_rgbw_int(int(color_b["value"]))
        raw_brightness = color_b.get("brightness")
        if isinstance(raw_brightness, (int, float)):
            brightness = clamp_byte(raw_brightness)
    elif isinstance(legacy_color, (int, float)):
        scene = "color"
        rgbw = full_scale_rgbw(int(legacy_color))
        brightness = get_brightness_from_rgbw_int(int(legacy_color))

    return CurrentlyPlaying(
        on_state=bool(playing.get("onState")),
        rgbw=rgbw,
        brightness=brightness,
        animation=animation,
        scene=scene,
        length_seconds=length_seconds,
        pattern=dict(pattern) if pattern is not None else None,
        architectural=dict(architectural) if architectural is not None else None,
        raw=dict(playing),
    )


def build_play_payload(command: Mapping[str, Any], *, origin: str = DEFAULT_ORIGIN) -> dict[str, Any]:
    """Wrap a currently-playing command in a desired-state shadow document."""
    return {"state": {"desired": {"origin": origin, "currentlyPlaying": dict(command)}}}


def _validate_name(name: str) -> str:
    """Check a scene name against the documented 1-32 character bound."""
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise ValueError(f"Name must be 1-{MAX_NAME_LENGTH} characters, got {name!r}")
    return name


def _scene_command(key: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Build a play command that sets one scene and clears every other.

    The API expects exactly one of ``colorB``, ``pattern`` or ``architectural``
    to be set with the others explicitly null, so that playing one clears any
    other. The legacy ``color`` field is nulled too: it is the shape the cloud
    and mobile app still speak, so a stale value left set on the controller is
    the one most likely to confuse another client.
    """
    command: dict[str, Any] = dict.fromkeys(_SCENE_KEYS)
    command[key] = dict(value)
    command["onState"] = True
    return command


def build_color_command(rgbw: RGBW, brightness: int) -> dict[str, Any]:
    """Build a play-colour command: one static colour at a brightness."""
    return _scene_command("colorB", {"value": rgbw_to_int(*rgbw), "brightness": clamp_byte(brightness)})


def _animation_for(slug: str) -> Animation:
    """Look an animation up, rejecting one the firmware would not know."""
    try:
        return CATALOG[slug]
    except KeyError:
        raise ValueError(f"Unknown animation: {slug!r}") from None


def build_pattern_command(
    animation: str,
    name: str,
    colors: Sequence[RGBW],
    brightness: int,
    *,
    speed: int = 128,
    direction: int | None = None,
    background_color: int = 0,
    extra_parameters: dict[int, int] | None = None,
) -> dict[str, Any]:
    """Build a play-pattern command shaped for this particular animation.

    Each animation reads its own fields, and sending the wrong set is how one
    ends up looking broken rather than failing (animations.py). So ``direction``
    is left out for the animations that take none, ``backgroundColor`` for those
    without one, and ``extraParameters`` is filled from the animation's own
    defaults for the twelve that read it.

    ``id`` and ``referencePatternId`` are documented as arbitrary but must be
    valid UUID v4 strings, so a fresh pair is minted per command. Colours past
    ``MAX_COLORS`` are dropped silently -- the slider-style inputs are clamped
    rather than rejected -- whereas an unknown animation or a bad ``name`` is a
    caller error and raises.
    """
    if not colors:
        raise ValueError("A pattern needs at least one colour")
    spec = _animation_for(animation)
    pattern: dict[str, Any] = {
        "id": str(uuid4()),
        "name": _validate_name(name),
        "colors": [rgbw_to_int(*color) for color in colors[:MAX_COLORS]],
        "animation": animation,
        "brightness": clamp_byte(brightness),
        "speed": clamp_speed(speed),
        "referencePatternId": str(uuid4()),
    }
    chosen = spec.direction_for(direction)
    if chosen is not None:
        pattern["direction"] = chosen
    extras = spec.extra_parameters(extra_parameters)
    if extras is not None:
        pattern["extraParameters"] = extras
    if spec.background_color:
        pattern["backgroundColor"] = background_color
    return _scene_command("pattern", pattern)


def build_pattern_replay_command(
    pattern: Mapping[str, Any],
    brightness: int | None = None,
    *,
    speed: int | None = None,
) -> dict[str, Any]:
    """Re-send a pattern the controller reported, with optional overrides.

    Used when the user moves the brightness slider, or the Animation speed
    control, while an animation is playing: replaying the reported object keeps
    the animation, its colours and every field this integration does not model
    -- including the ``extraParameters`` a pattern built in the app carries --
    changing only what was asked for.
    """
    replay = dict(pattern)
    if brightness is not None:
        replay["brightness"] = clamp_byte(brightness)
    if speed is not None:
        replay["speed"] = clamp_speed(speed)
    return _scene_command("pattern", replay)


def build_architectural_replay_command(
    architectural: Mapping[str, Any], brightness: int | None = None
) -> dict[str, Any]:
    """Re-send an architectural scene the controller reported, optionally at a new brightness.

    The architectural mirror of ``build_pattern_replay_command``: a brightness
    move must not collapse a per-pixel design into one solid colour.
    """
    replay = dict(architectural)
    if brightness is not None:
        replay["brightness"] = clamp_byte(brightness)
    return _scene_command("architectural", replay)


def build_on_state_command(on_state: bool) -> dict[str, Any]:
    """Build a command that only toggles power, leaving the scene untouched."""
    return {"onState": on_state}


class GemstoneClient:
    """Talks to one controller's local HTTP server."""

    def __init__(
        self,
        host: str,
        session: aiohttp.ClientSession,
        *,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        origin: str = DEFAULT_ORIGIN,
    ) -> None:
        """Store connection details. The session is owned by the caller."""
        self.host = host
        self.port = port
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._origin = origin
        # The controller serves HTTP from the same firmware that drives the
        # pixels and documents a 503 for "system busy". Serialising our own
        # traffic keeps a routine poll from colliding with a user's command.
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        """Root URL of the controller's HTTP server."""
        return f"http://{self.host}:{self.port}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform one request, serialised, retrying once on a busy controller."""
        url = f"{self.base_url}{path}"
        async with self._lock:
            attempt = 0
            while True:
                attempt += 1
                try:
                    async with self._session.request(method, url, json=json, timeout=self._timeout) as response:
                        if response.status == HTTPStatus.SERVICE_UNAVAILABLE:
                            if attempt == 1:
                                _LOGGER.debug("%s busy, retrying %s %s", self.host, method, path)
                                await asyncio.sleep(BUSY_RETRY_DELAY)
                                continue
                            raise GemstoneBusyError(ERROR_MESSAGES[503])
                        if response.status != HTTPStatus.OK:
                            raise GemstoneResponseError(response.status)
                        # An embedded server does not reliably set a JSON
                        # content type, so do not let aiohttp police it.
                        body = await response.json(content_type=None)
                # Caught before ClientError: aiohttp's ServerTimeoutError
                # subclasses both, and a timeout is the more specific diagnosis.
                except TimeoutError as err:
                    raise GemstoneConnectionError(f"{self.host} did not respond in time") from err
                except aiohttp.ClientError as err:
                    raise GemstoneConnectionError(f"Could not reach {self.host}: {err}") from err
                except ValueError as err:
                    raise GemstoneParseError(f"{self.host} returned invalid JSON: {err}") from err

                if not isinstance(body, Mapping):
                    raise GemstoneParseError(f"Expected a JSON object, got {type(body).__name__}")
                return dict(body)

    async def async_get_hub_settings(self) -> HubSettings:
        """Read controller configuration -- firmware, pixel count, local IP."""
        return parse_hub_settings(await self._request("GET", PATH_HUB_SETTINGS))

    async def async_get_currently_playing(self) -> CurrentlyPlaying:
        """Read what the controller is currently displaying."""
        return parse_currently_playing(await self._request("GET", PATH_CURRENTLY_PLAYING))

    async def async_play(self, command: Mapping[str, Any]) -> CurrentlyPlaying:
        """Send a currently-playing command and return the resulting state."""
        payload = build_play_payload(command, origin=self._origin)
        return parse_currently_playing(await self._request("POST", PATH_PLAY, json=payload))

    async def async_play_color(self, rgbw: RGBW, brightness: int) -> CurrentlyPlaying:
        """Play a static colour at the given brightness."""
        return await self.async_play(build_color_command(rgbw, brightness))

    async def async_play_pattern(
        self,
        animation: str,
        name: str,
        colors: Sequence[RGBW],
        brightness: int,
        *,
        speed: int = 128,
        background_color: int = 0,
    ) -> CurrentlyPlaying:
        """Play an animation using the given colours."""
        return await self.async_play(
            build_pattern_command(
                animation,
                name,
                colors,
                brightness,
                speed=speed,
                background_color=background_color,
            )
        )

    async def async_set_on_state(self, on_state: bool) -> CurrentlyPlaying:
        """Turn the lights on or off without changing the running scene."""
        return await self.async_play(build_on_state_command(on_state))
