#!/usr/bin/env python3
"""A fake Gemstone Hub2 controller, so the integration can be driven in a browser.

The offline tests mock at the aiohttp-session boundary, which leaves the whole
Home Assistant UI surface unverified: the config flow's error copy, the entry
title, the device page, the colour wheel, the effect picker, the options flow,
and every timing behaviour in the coordinator. This serves the three endpoints
`api.py` actually calls, over a real socket, so a local Home Assistant instance
can talk to it exactly as it would talk to hardware.

Two design rules keep it honest:

* It is a *truthful* oracle, not a permissive one. It rejects what the API
  document says the firmware rejects (405, 411, 413, 415, 505) and *assumes* a
  400 for values outside the document's property table (unverified on hardware;
  Q5 in docs/design.md), because a fake more lenient than the device is how a
  bug ships. Where the document is silent, the assumption is named in the code.
* Its wire shapes come from the same place the tests' do -- the API document and
  `tests/payloads.py`. `tests/test_hub2_sim.py` round-trips this module's output
  through the real parsers to stop it drifting into a fake that only agrees with
  itself, and feeds every command `api.py` can build through `validate_command`.

It models every capability the document describes: the full hub-settings shape
(per-output counts and names), every scene shape including the opaque
`playlist` and `impulse`, the BLE `B800`/`B801` enable/disable of the HTTP
server with the power cycle the document requires, a reboot that comes back on
new firmware, and every documented error code. On top of that it offers timing
and failure profiles -- latency, flaky Wi-Fi, lossy links, busy bursts -- and
both readings of how the firmware might treat an out-of-range value.

It defaults to port 8080 so it needs no privileges:

    .venv/bin/python tools/hub2_sim.py
    .venv/bin/python tools/hub2_sim.py --controller "Front Roofline:8080" --controller "Garage:8081"

Add it in Home Assistant with host `127.0.0.1` and port `8080`. Firmware itself
always serves on 80, which is why that is the config flow's default -- but the
port is a field, so nothing here has to bind a privileged socket.

It also speaks the controller's side of Control4 SDDP (`sddp.py`): it answers
`SEARCH * SDDP/1.0` on UDP 1902 with the firmware's exact text, requires the
same `From`/`Tran`/`Timeout` headers the firmware does, and multicasts
`NOTIFY ALIVE` every five minutes and `NOTIFY OFFLINE` on exit. One extension:
when its HTTP port is not 80 it adds an `Http-Port:` header, which the
integration reads so several simulators on one address stay distinguishable.
To see discovery from a Home Assistant on the same machine or the LAN, bind
to a reachable address -- `--host <this machine's LAN address>` -- because a
discovered controller is confirmed over HTTP at the address the reply came
from, and `127.0.0.1` is not that address.

Everything is driven over HTTP at `/_sim/*` rather than by keystrokes, so
scenarios are scriptable and the process survives being backgrounded. Open
http://127.0.0.1:8080/_sim for a control panel and a live request log.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import random
import re
import socket
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from aiohttp import HttpVersion11, web

# Paths the integration calls (the PATH_* constants in api.py), plus the aliases
# the API document lists for the same play handler.
PATH_HUB_SETTINGS: Final = "/device-state/hub-settings"
PATH_CURRENTLY_PLAYING: Final = "/device-state/currently-playing"
PATH_PLAY: Final = "/device-control/play"
PLAY_ALIASES: Final = ("/play", "/play-colors", "/play-patterns", "/play-architectural", "/play-on")

# Exactly one of these is set at a time; a play command that names any of them
# replaces all of them. `playlist` and `impulse` are in every documented
# payload but never defined beyond an orphan `lengthInSeconds`.
SCENE_KEYS: Final = ("color", "colorB", "pattern", "architectural", "playlist", "impulse")

# The ecosystem's default blue, packed as (w << 24) + (b << 16) + (g << 8) + r.
# Red lives in the *lowest* byte, so pure red is 255, not 16711680.
DEFAULT_BLUE: Final = 12614435

MAX_LOG_ENTRIES: Final = 100
SCRIPT_PATH: Final = Path(__file__).resolve()
OUTPUTS: Final = 4

# The document says 413 means "payload exceeds buffer size" and never gives the
# size. ASSUMED: 8 KiB. A 20-colour pattern is about 600 bytes, so only a
# deliberate test reaches this.
MAX_BODY_BYTES: Final = 8 * 1024

# Bounds from the API document's property table.
MAX_COLOR_INT: Final = 2**32 - 1
MAX_NAME_LENGTH: Final = 32
MAX_COLORS: Final = 20
# Six directions exist, and no animation acts on anything above 5. The local
# API document bounds the field more loosely; this follows the animations.
MAX_DIRECTION: Final = 5
MAX_LENGTH_SECONDS: Final = 86400

FAULT_MODES: Final = (
    "normal",
    "busy",
    "busy-once",
    "busy-burst",
    "slow",
    "latency",
    "down",
    "flaky",
    "lossy",
    "error",
    "unauthorized",
)
REPLY_MODES: Final = ("applied", "accepted", "on-state-only")
VALIDATION_MODES: Final = ("reject", "clamp")
BLE_COMMANDS: Final = ("B800", "B801")

# The full hub-settings shape a real Hub2 returns -- the document's example
# shows only `location` and `pixelCount`, but the reply captured in
# tests/test_api.py carries all of this. Two populated outputs, named, so
# per-output handling is exercised.
HUB_SETTINGS: Final[dict[str, Any]] = {
    "pixelCount": [104, 52, 0, 0],
    "reversePixels": [False, False, False, False],
    "pixelOutputNames": ["Front roofline", "Garage", "", ""],
    "localIp": "127.0.0.1",
    "rgbwSequence": "RGBW",
    "timeZone": "-07:00",
    "dstActive": "true",
    "dstMode": "auto",
    # The config flow titles the entry from this, so it is how you tell at a
    # glance that setup read the device rather than falling back to the host.
    "bluetoothName": "Front Roofline",
    # The "Allow Local Commands" flag. Toggled over BLE (B801/B800); see
    # Simulator.ble_command for the power-cycle rule.
    "tcpEnabled": True,
    "location": {"name": "Calgary, AB", "lat": 51.05, "long": -114.07},
    # Becomes the device page's sw_version (DeviceInfo in entity.py).
    "firmware": "1.1.5",
    "firmwareSpi": "1.0.2",
    "firmwareWifi": "1.0.7",
    "network": {"interface": "wifi-sim0", "preferred": "auto"},
}

PROFILES: Final[dict[str, dict[str, Any]]] = {
    "color-on": {
        "color": None,
        "colorB": {"value": DEFAULT_BLUE, "brightness": 180},
        "pattern": None,
        "architectural": None,
        "onState": True,
    },
    "off": {
        "color": None,
        "colorB": {"value": DEFAULT_BLUE, "brightness": 180},
        "pattern": None,
        "architectural": None,
        "onState": False,
    },
    # Reaching this by clicking is awkward, but it is the state that exercises
    # the brightness-slider-while-animating branch (pattern replay) in light.py.
    "pattern-playing": {
        "color": None,
        "colorB": None,
        "pattern": {
            "id": "8d8b1e70-4ed4-439e-b096-52446653d758",
            "name": "IsoFade",
            "colors": [255, 65280, 16711680],
            "animation": "isofade",
            "brightness": 200,
            "speed": 128,
            "direction": 0,
            "referencePatternId": "93a145b7-e526-4a3c-b315-b1dbb6193eab",
            "backgroundColor": 0,
        },
        "architectural": None,
        "onState": True,
    },
    "architectural-playing": {
        "color": None,
        "colorB": None,
        "pattern": None,
        # Section 3.3 of the Control4 document. Indices stay below 104 so they
        # fit the first output under every reading of the index space.
        "architectural": {
            "name": "Front Roofline Design",
            "id": "3d3f33d3-638f-43b4-ac5f-3efdde6bd0b9",
            "preview": False,
            "brightness": 255,
            "staticColors": [
                {"color": 12614435, "lights": [4, 8, 9, 11, 19, 29, 30, 36, 38, 39]},
                {"color": 26367, "lights": [6, 13]},
                {"color": 110049, "lights": [16, 17, 24, 25]},
            ],
        },
        "onState": True,
    },
    # ASSUMED shapes. The document names these scenes and bounds
    # `lengthInSeconds` (1-86400); it defines nothing else. These carry the one
    # documented field and a name, so Home Assistant sees the scene kind and
    # nothing that could be mistaken for firmware behaviour.
    "playlist-playing": {
        "color": None,
        "colorB": None,
        "pattern": None,
        "architectural": None,
        "playlist": {"name": "Evening", "lengthInSeconds": 30},
        "impulse": None,
        "onState": True,
    },
    "impulse-playing": {
        "color": None,
        "colorB": None,
        "pattern": None,
        "architectural": None,
        "playlist": None,
        "impulse": {"name": "Doorbell", "lengthInSeconds": 5},
        "onState": True,
    },
}

# Cycled by /_sim/external to imitate someone using the phone app.
EXTERNAL_COLORS: Final = (
    (255, "red"),
    (65280, "green"),
    (16711680, "blue"),
    (16777215, "white"),
    (DEFAULT_BLUE, "gemstone blue"),
)


def reported(body: dict[str, Any], origin: str = "http") -> dict[str, Any]:
    """Wrap a payload in a shadow document.

    Requests carry `state.desired`; responses carry `state.reported`. Firmware
    confirmed it echoes back whatever `origin` the request set, defaulting to
    "http" -- which is what makes Home Assistant's own traffic identifiable in
    the request log below.
    """
    return {"state": {"reported": {**body, "origin": origin, "env": "prod"}}}


def apply_command(state: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    """Merge a desired `currentlyPlaying` into the current scene.

    A bare `{"onState": false}` -- what `build_on_state_command` sends for a
    power toggle -- must leave the scene untouched, matching COLOR_OFF_RESPONSE
    in tests/payloads.py. Anything that names a scene key replaces all of them,
    because the API requires exactly one to be set and the rest explicitly null.
    What the *reply* to a power toggle carries is a separate question; see the
    `on-state-only` reply mode.
    """
    updated = dict(state)
    if any(key in command for key in SCENE_KEYS):
        for key in SCENE_KEYS:
            updated[key] = command.get(key)
    if "onState" in command:
        updated["onState"] = bool(command["onState"])
    return updated


def _int_in(value: Any, low: int, high: int) -> bool:
    """Accept a real integer within bounds; ``bool`` is an int subclass and is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _extra_parameters_problem(extras: Any) -> str | None:
    """Check the shape of an ``extraParameters`` object.

    Which indexes belong to which animation is the caller's business -- the
    catalog lives in the integration. What is checked here is the wire format:
    a version, and every other key a numeric index carrying a whole value and a
    name. A half-built object is the failure worth catching, because firmware
    fills the gaps silently.
    """
    if not isinstance(extras, dict):
        return "pattern.extraParameters must be an object"
    if not _int_in(extras.get("version"), 0, 2**16):
        return "pattern.extraParameters.version must be a non-negative integer"
    for key, entry in extras.items():
        if key == "version":
            continue
        if not key.isdigit():
            return f"pattern.extraParameters key {key!r} must be a numeric index"
        if not isinstance(entry, dict):
            return f"pattern.extraParameters[{key}] must be an object"
        if not _int_in(entry.get("value"), 0, 2**32 - 1):
            return f"pattern.extraParameters[{key}].value must be an integer"
        if "name" in entry and not isinstance(entry["name"], str):
            return f"pattern.extraParameters[{key}].name must be a string"
    return None


def _name_ok(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= MAX_NAME_LENGTH


def validate_command(command: dict[str, Any]) -> str | None:
    """Return why the firmware would reject a command, or None if it would not.

    Every bound here comes from the API document's property table. Its 400
    row, though, lists only malformed JSON and a missing body: that the firmware
    answers an out-of-range *value* with 400 rather than clamping or ignoring
    it is an assumption this simulator makes, so that a client bug fails loudly
    on the bench instead of silently. `clamp_command` is the other reading.
    Verify on hardware. Unknown keys are left alone -- the document says the
    endpoints are still changing.
    """
    active = [key for key in SCENE_KEYS if command.get(key) is not None]
    if len(active) > 1:
        return f"Exactly one scene may be set, got {active}"
    if "onState" in command and not isinstance(command["onState"], bool):
        return "onState must be a boolean"

    pattern = command.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, dict):
            return "pattern must be an object"
        if not _name_ok(pattern.get("name")):
            return f"pattern.name must be 1-{MAX_NAME_LENGTH} characters"
        if not isinstance(pattern.get("animation"), str):
            return "pattern.animation must be a string"
        colors = pattern.get("colors")
        if not isinstance(colors, list) or not 1 <= len(colors) <= MAX_COLORS:
            return f"pattern.colors must be a list of 1-{MAX_COLORS} colours"
        if not all(_int_in(color, 0, MAX_COLOR_INT) for color in colors):
            return "pattern.colors entries must be 32-bit RGBW integers"
        if "brightness" in pattern and not _int_in(pattern["brightness"], 0, 255):
            return "pattern.brightness must be 0-255"
        # Speed starts at 1: 0 is outside what the animations accept.
        if "speed" in pattern and not _int_in(pattern["speed"], 1, 255):
            return "pattern.speed must be 1-255"
        if "direction" in pattern and not _int_in(pattern["direction"], 0, MAX_DIRECTION):
            return f"pattern.direction must be 0-{MAX_DIRECTION}"
        if "backgroundColor" in pattern and not _int_in(pattern["backgroundColor"], 0, MAX_COLOR_INT):
            return "pattern.backgroundColor must be a 32-bit RGBW integer"
        if "extraParameters" in pattern:
            problem = _extra_parameters_problem(pattern["extraParameters"])
            if problem is not None:
                return problem

    architectural = command.get("architectural")
    if architectural is not None:
        if not isinstance(architectural, dict):
            return "architectural must be an object"
        if not _name_ok(architectural.get("name")):
            return f"architectural.name must be 1-{MAX_NAME_LENGTH} characters"
        if "brightness" in architectural and not _int_in(architectural["brightness"], 0, 255):
            return "architectural.brightness must be 0-255"
        static_colors = architectural.get("staticColors")
        if not isinstance(static_colors, list) or not static_colors:
            return "architectural.staticColors must be a non-empty list"
        for segment in static_colors:
            if not isinstance(segment, dict):
                return "architectural.staticColors entries must be objects"
            lights = segment.get("lights")
            if not isinstance(lights, list) or not lights or not all(_int_in(i, 0, MAX_COLOR_INT) for i in lights):
                return "architectural.staticColors[].lights must be a non-empty list of non-negative integers"
            if not _int_in(segment.get("color"), 0, MAX_COLOR_INT):
                return "architectural.staticColors[].color must be a 32-bit RGBW integer"

    color_b = command.get("colorB")
    if color_b is not None:
        if not isinstance(color_b, dict):
            return "colorB must be an object"
        if not _int_in(color_b.get("value"), 0, MAX_COLOR_INT):
            return "colorB.value must be a 32-bit RGBW integer"
        if not _int_in(color_b.get("brightness"), 0, 255):
            return "colorB.brightness must be 0-255"

    # Opaque scenes: the document defines nothing but the duration bound.
    for key in ("playlist", "impulse"):
        opaque = command.get(key)
        if opaque is None:
            continue
        if not isinstance(opaque, dict):
            return f"{key} must be an object"
        if "lengthInSeconds" in opaque and not _int_in(opaque["lengthInSeconds"], 1, MAX_LENGTH_SECONDS):
            return f"{key}.lengthInSeconds must be 1-{MAX_LENGTH_SECONDS}"
    return None


def _clamp_int(value: Any, low: int, high: int) -> Any:
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    return max(low, min(high, value))


def clamp_command(command: dict[str, Any]) -> dict[str, Any]:
    """Bring out-of-range *values* into bounds instead of rejecting: the other reading of Q5.

    Range violations are clamped and over-long lists and names truncated;
    structural problems (wrong types, missing fields, two scenes at once) are
    left alone, so `validate_command` still rejects them afterwards. This is a
    hypothesis about the firmware, not observed behaviour.
    """
    clamped = copy.deepcopy(command)
    pattern = clamped.get("pattern")
    if isinstance(pattern, dict):
        for field in ("brightness", "speed"):
            if field in pattern:
                pattern[field] = _clamp_int(pattern[field], 0, 255)
        if "direction" in pattern:
            pattern["direction"] = _clamp_int(pattern["direction"], 0, MAX_DIRECTION)
        if "backgroundColor" in pattern:
            pattern["backgroundColor"] = _clamp_int(pattern["backgroundColor"], 0, MAX_COLOR_INT)
        if isinstance(pattern.get("colors"), list):
            pattern["colors"] = [_clamp_int(c, 0, MAX_COLOR_INT) for c in pattern["colors"][:MAX_COLORS]]
        if isinstance(pattern.get("name"), str):
            pattern["name"] = pattern["name"][:MAX_NAME_LENGTH]
    architectural = clamped.get("architectural")
    if isinstance(architectural, dict):
        if "brightness" in architectural:
            architectural["brightness"] = _clamp_int(architectural["brightness"], 0, 255)
        if isinstance(architectural.get("name"), str):
            architectural["name"] = architectural["name"][:MAX_NAME_LENGTH]
    color_b = clamped.get("colorB")
    if isinstance(color_b, dict):
        if "brightness" in color_b:
            color_b["brightness"] = _clamp_int(color_b["brightness"], 0, 255)
        if "value" in color_b:
            color_b["value"] = _clamp_int(color_b["value"], 0, MAX_COLOR_INT)
    for key in ("playlist", "impulse"):
        opaque = clamped.get(key)
        if isinstance(opaque, dict) and "lengthInSeconds" in opaque:
            opaque["lengthInSeconds"] = _clamp_int(opaque["lengthInSeconds"], 1, MAX_LENGTH_SECONDS)
    return clamped


def describe(state: dict[str, Any]) -> str:
    """One-line summary of a scene, for the request log."""
    power = "on" if state.get("onState") else "off"
    pattern = state.get("pattern")
    if isinstance(pattern, dict):
        return f"{power} pattern={pattern.get('animation')}@{pattern.get('brightness')}"
    color_b = state.get("colorB")
    if isinstance(color_b, dict):
        return f"{power} colorB={color_b.get('value')}@{color_b.get('brightness')}"
    architectural = state.get("architectural")
    if isinstance(architectural, dict):
        return f"{power} architectural={architectural.get('name')}@{architectural.get('brightness')}"
    for key in ("playlist", "impulse"):
        opaque = state.get(key)
        if isinstance(opaque, dict):
            return f"{power} {key}={opaque.get('name')} ({opaque.get('lengthInSeconds')}s)"
    return power


class Simulator:
    """One controller: its scene, settings, injected faults, and request log.

    Timed behaviours -- a power cycle, a reboot, a flaky link -- are evaluated
    lazily on each request from their start time rather than run on timers, so
    a simulator never leaves a task behind. The only task is the `accepted`
    reply mode's convergence, tracked and cancelled by `cancel_pending`.
    """

    def __init__(self, profile: str, name: str | None = None, serial: str | None = None) -> None:
        """Seed the scene from a named profile."""
        self.state: dict[str, Any] = dict(PROFILES[profile])
        self.hub_settings: dict[str, Any] = copy.deepcopy(HUB_SETTINGS)
        if name is not None:
            self.hub_settings["bluetoothName"] = name
        # What SDDP advertises as Gemstone-<serial>; firmware uses the unit's
        # serial number, which is not in hub-settings, so it lives here alone.
        self.serial: str = serial or "SIM0001"
        # Where this simulator's HTTP lives, for the Http-Port SDDP extension.
        self.http_port: int = 80

        self.fault: str = "normal"
        self.slow_seconds: float = 0.0
        # Independent of `fault`: a latency profile composes with any fault.
        self.latency: dict[str, float] | None = None
        self._latency_rng = random.Random()
        self.burst_remaining: int = 0
        self.flaky: dict[str, float] | None = None
        self.lossy: dict[str, float] | None = None
        self._lossy_rng = random.Random()

        # "applied": the play reply reflects state the controller has already
        # applied. "accepted": it reflects a command merely queued, and the
        # device converges later. The coordinator trusts the reply either way,
        # so this toggle is what makes the difference visible. "on-state-only":
        # a power toggle's reply carries nothing but onState, exactly as the
        # document's section 3.4 shows -- the default replies with the whole
        # scene, which is kinder than the document promises.
        self.reply_mode: str = "applied"
        self.apply_delay: float = 8.0
        # "reject": out-of-range values get a 400 (assumed). "clamp": they are
        # brought into bounds and applied (the other hypothesis).
        self.validation_mode: str = "reject"

        # The HTTP server itself. `B800` clears tcpEnabled but, per the
        # document, the server stays up until a power cycle; `B801` brings it
        # up at once. While booting nothing answers.
        self.http_up: bool = True
        self.booting_until: float | None = None
        self._pending_firmware: dict[str, str] = {}

        self._pending: asyncio.Task[None] | None = None
        self.log: deque[dict[str, Any]] = deque(maxlen=MAX_LOG_ENTRIES)
        self._last_device_request: float | None = None
        self._external_index = 0

    # --- request log -------------------------------------------------------

    def record(self, method: str, path: str, status: int, origin: str, note: str) -> None:
        """Append one request to the log and print it.

        The delta is measured only between *device* requests, so `/_sim` traffic
        does not pollute the picture. That delta is the whole point: it is what
        makes the 5s fast window, the 30s idle interval and the backoff ladder
        visible from outside Home Assistant.
        """
        now = time.monotonic()
        delta: float | None = None
        if not path.startswith("/_sim"):
            if self._last_device_request is not None:
                delta = now - self._last_device_request
            self._last_device_request = now

        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        entry = {
            "time": stamp,
            "method": method,
            "path": path,
            "status": status,
            "delta": round(delta, 2) if delta is not None else None,
            "origin": origin,
            "note": note,
        }
        self.log.append(entry)

        gap = f"Δ{delta:6.2f}s" if delta is not None else " " * 8
        print(f"{stamp}  {method:4} {path:34} {status}  {gap}  {origin:14} {note}", flush=True)

    # --- accepted-reply convergence ----------------------------------------

    def schedule_apply(self, command_state: dict[str, Any]) -> None:
        """In `accepted` mode, converge to the commanded state after a delay."""
        self.cancel_pending()

        async def converge() -> None:
            await asyncio.sleep(self.apply_delay)
            self.state = command_state
            self.record("--", "(device converged)", 0, "device", describe(command_state))

        self._pending = asyncio.create_task(converge())

    def cancel_pending(self) -> None:
        """Drop any scheduled convergence, so shutdown leaves no live timer."""
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        self._pending = None

    # --- scene ---------------------------------------------------------------

    def external_change(self) -> str:
        """Imitate the phone app changing the lights behind Home Assistant's back.

        GemstoneCoordinator._async_update_data treats any polled change it did
        not cause as activity, which should collapse the poll interval back to 5s for a
        minute. This is the one scenario a startup flag cannot express.
        """
        value, name = EXTERNAL_COLORS[self._external_index % len(EXTERNAL_COLORS)]
        self._external_index += 1
        self.state = apply_command(
            self.state,
            {
                "color": None,
                "colorB": {"value": value, "brightness": 200},
                "pattern": None,
                "architectural": None,
                "onState": True,
            },
        )
        return name

    # --- capabilities: BLE, power, firmware ------------------------------

    def ble_command(self, code: str) -> None:
        """Apply a BLE command exactly as the document describes it.

        `B801` enables the HTTP server and it answers at once. `B800` disables
        it, but "you must power-cycle the controller for the change to take
        effect" -- so the flag flips now and the server keeps serving until
        `power_cycle`.
        """
        if code == "B801":
            self.hub_settings["tcpEnabled"] = True
            self.http_up = True
        elif code == "B800":
            self.hub_settings["tcpEnabled"] = False
        else:
            raise ValueError(f"Unknown BLE command: {code}")

    def power_cycle(self, seconds: float, firmware: dict[str, str] | None = None) -> None:
        """Go dark for `seconds`, then come back -- on new firmware if given.

        ASSUMED: the scene survives a power cycle (the controller has schedules
        and restores what it was showing). Whether the HTTP server comes back
        follows `tcpEnabled`, which is what the document's B800 rule means.
        """
        self.cancel_pending()
        self.booting_until = time.monotonic() + max(0.0, seconds)
        self._pending_firmware = dict(firmware or {})

    def tick(self) -> None:
        """Advance lazily evaluated state; called before every device request."""
        now = time.monotonic()
        if self.booting_until is not None and now >= self.booting_until:
            self.booting_until = None
            for key, version in self._pending_firmware.items():
                self.hub_settings[key] = version
            self._pending_firmware = {}
            self.http_up = bool(self.hub_settings.get("tcpEnabled"))
        if self.flaky is not None:
            elapsed = now - self.flaky["start"]
            period = self.flaky["down"] + self.flaky["up"]
            if period <= 0 or elapsed >= self.flaky["cycles"] * period:
                self.flaky = None
                if self.fault == "flaky":
                    self.fault = "normal"

    def is_booting(self) -> bool:
        """Report whether the device is mid power-cycle."""
        return self.booting_until is not None and time.monotonic() < self.booting_until

    def flaky_down(self) -> bool:
        """Report whether the flaky schedule is in a down window."""
        if self.flaky is None:
            return False
        elapsed = time.monotonic() - self.flaky["start"]
        period = self.flaky["down"] + self.flaky["up"]
        return period > 0 and (elapsed % period) < self.flaky["down"]

    # --- timing and failure profiles ------------------------------------------

    def set_latency(self, mean: float, jitter: float, seed: int | None = None) -> None:
        """Set a per-request delay profile, seeded for repeatability."""
        self.latency = {"mean": max(0.0, mean), "jitter": max(0.0, jitter)}
        self._latency_rng = random.Random(seed)

    def next_latency(self) -> float:
        """Draw the next delay from the latency profile."""
        if self.latency is None:
            return 0.0
        return max(0.0, self._latency_rng.gauss(self.latency["mean"], self.latency["jitter"]))

    def set_lossy(self, rate: float, seed: int | None = None) -> None:
        """Set the per-request drop probability, seeded for repeatability."""
        self.lossy = {"rate": min(1.0, max(0.0, rate))}
        self._lossy_rng = random.Random(seed)

    def lossy_drop(self) -> bool:
        """Decide whether the lossy link drops this request."""
        return self.lossy is not None and self._lossy_rng.random() < self.lossy["rate"]

    def clear_faults(self) -> None:
        """Return to normal service: no fault, no latency, no schedule."""
        self.fault = "normal"
        self.slow_seconds = 0.0
        self.latency = None
        self.burst_remaining = 0
        self.flaky = None
        self.lossy = None

    # --- status ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Summarise the controller for the panel and for scripts."""
        self.tick()
        boot_left = max(0.0, self.booting_until - time.monotonic()) if self.booting_until is not None else 0.0
        return {
            "name": self.hub_settings.get("bluetoothName"),
            "fault": self.fault,
            "slow_seconds": self.slow_seconds,
            "latency": self.latency,
            "flaky": self.flaky,
            "lossy": self.lossy,
            "busy_burst_remaining": self.burst_remaining,
            "reply_mode": self.reply_mode,
            "apply_delay": self.apply_delay,
            "validation_mode": self.validation_mode,
            "tcp_enabled": self.hub_settings.get("tcpEnabled"),
            "http_up": self.http_up and not self.is_booting(),
            "booting_seconds_left": round(boot_left, 1),
            "hub_settings": {
                "firmware": self.hub_settings.get("firmware"),
                "firmwareSpi": self.hub_settings.get("firmwareSpi"),
                "firmwareWifi": self.hub_settings.get("firmwareWifi"),
                "pixelCount": self.hub_settings.get("pixelCount"),
                "pixelOutputNames": self.hub_settings.get("pixelOutputNames"),
            },
            "state": self.state,
            "log": list(self.log),
        }


def _abort(request: web.Request, note: str) -> web.HTTPException:
    """Drop the connection the way a dead or rebooting device would.

    An abrupt close, not a status code: this is the path that produces
    GemstoneConnectionError rather than GemstoneResponseError.
    """
    request["note"] = note
    if request.transport is not None:
        request.transport.abort()
    return web.HTTPServiceUnavailable(text="offline")


def build_app(sim: Simulator) -> web.Application:
    """Wire up middleware and routes.

    Middleware order matters: logging is outermost so every request is recorded,
    then the device's state and faults, then request validation. Both exempt
    `/_sim/*` -- if "always 503" applied to the control plane it would brick the
    only way out, leaving `pkill` as the escape hatch.
    """

    @web.middleware
    async def log_mw(request: web.Request, handler: Any) -> web.StreamResponse:
        request["origin"] = "-"
        request["note"] = ""
        try:
            response = await handler(request)
        except web.HTTPException as err:
            sim.record(request.method, request.path, err.status, request.get("origin", "-"), request.get("note", ""))
            raise
        sim.record(request.method, request.path, response.status, request.get("origin", "-"), request.get("note", ""))
        return response

    @web.middleware
    async def device_mw(request: web.Request, handler: Any) -> web.StreamResponse:
        """Apply the device's power state, the documented server limits, then the injected fault."""
        if request.path.startswith("/_sim"):
            return await handler(request)

        sim.tick()
        if sim.is_booting():
            raise _abort(request, "rebooting")
        if not sim.http_up:
            raise _abort(request, "HTTP server disabled (B800 + power cycle)")

        # Documented: "Only HTTP/1.1 supported" and "payload exceeds buffer size".
        if request.version < HttpVersion11:
            raise web.HTTPVersionNotSupported(text="Only HTTP/1.1 is supported")
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY_BYTES, actual_size=request.content_length)

        if sim.fault == "down":
            raise _abort(request, "connection aborted")
        if sim.fault == "flaky" and sim.flaky_down():
            raise _abort(request, "flaky: down window")
        if sim.fault == "lossy" and sim.lossy_drop():
            raise _abort(request, "lossy: dropped")

        if sim.fault in ("busy", "busy-once", "busy-burst"):
            if sim.fault == "busy-once":
                sim.fault = "normal"
                request["note"] = "503 once, then normal"
            elif sim.fault == "busy-burst":
                sim.burst_remaining -= 1
                request["note"] = f"503 burst, {sim.burst_remaining} left"
                if sim.burst_remaining <= 0:
                    sim.fault = "normal"
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Service temporarily unavailable"}),
                content_type="application/json",
            )
        if sim.fault == "error":
            request["note"] = "500 injected"
            raise web.HTTPInternalServerError(text="Internal server error")
        if sim.fault == "unauthorized":
            request["note"] = "401 injected (access control is a future expansion)"
            raise web.HTTPUnauthorized(text="Unauthorized")

        if sim.fault == "slow":
            request["note"] = f"delayed {sim.slow_seconds:g}s"
            await asyncio.sleep(sim.slow_seconds)
        if sim.latency is not None:
            delay = sim.next_latency()
            request["note"] = f"latency {delay:.2f}s"
            await asyncio.sleep(delay)

        return await handler(request)

    @web.middleware
    async def validate_mw(request: web.Request, handler: Any) -> web.StreamResponse:
        """Reject what the API document says the firmware rejects."""
        if request.path.startswith("/_sim"):
            return await handler(request)
        if request.method not in ("GET", "POST"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
        if request.method == "POST":
            if request.content_length is None:
                raise web.HTTPLengthRequired(text="Missing Content-Length header")
            if request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType(text="Only application/json is supported")
        return await handler(request)

    # --- device routes ----------------------------------------------------------

    async def get_hub_settings(request: web.Request) -> web.Response:
        counts = sim.hub_settings.get("pixelCount", [])
        request["note"] = f"firmware={sim.hub_settings.get('firmware')} outputs={counts}"
        return web.json_response(reported({"hubSettings": sim.hub_settings}))

    async def get_currently_playing(request: web.Request) -> web.Response:
        request["note"] = describe(sim.state)
        return web.json_response(reported({"currentlyPlaying": sim.state}))

    async def post_play(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid JSON") from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object")

        desired = body.get("state", {}).get("desired", {})
        if not isinstance(desired, dict):
            raise web.HTTPBadRequest(text="Expected state.desired to be an object")
        command = desired.get("currentlyPlaying")
        if not isinstance(command, dict):
            raise web.HTTPBadRequest(text="Expected state.desired.currentlyPlaying to be an object")

        origin = desired.get("origin") or "http"
        request["origin"] = str(origin)

        clamped_note = ""
        if sim.validation_mode == "clamp":
            adjusted = clamp_command(command)
            if adjusted != command:
                clamped_note = " (clamped)"
            command = adjusted
        if (reason := validate_command(command)) is not None:
            request["note"] = f"rejected: {reason}"
            raise web.HTTPBadRequest(text=reason)

        commanded = apply_command(sim.state, command)
        if sim.reply_mode == "accepted":
            # Reply with what was accepted while the device still reports the
            # old scene, then converge. Home Assistant adopts the reply, so the
            # UI snaps forward and the next poll yanks it back.
            sim.schedule_apply(commanded)
            request["note"] = f"accepted -> {describe(commanded)} (applies in {sim.apply_delay:g}s){clamped_note}"
            return web.json_response(reported({"currentlyPlaying": commanded}, origin=str(origin)))

        sim.state = commanded
        request["note"] = describe(commanded) + clamped_note
        if sim.reply_mode == "on-state-only" and not any(key in command for key in SCENE_KEYS):
            request["note"] += " (reply: onState only)"
            bare = {"onState": commanded["onState"]}
            return web.json_response(reported({"currentlyPlaying": bare}, origin=str(origin)))
        return web.json_response(reported({"currentlyPlaying": commanded}, origin=str(origin)))

    # --- control plane ----------------------------------------------------------

    def _float(request: web.Request, key: str, default: float) -> float:
        try:
            return float(request.query.get(key, default))
        except ValueError:
            raise web.HTTPBadRequest(text=f"{key} must be a number") from None

    def _seed(request: web.Request) -> int | None:
        raw = request.query.get("seed")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            raise web.HTTPBadRequest(text="seed must be an integer") from None

    async def sim_mode(request: web.Request) -> web.Response:
        mode = request.match_info["mode"]
        if mode not in FAULT_MODES:
            raise web.HTTPBadRequest(text=f"Unknown mode: {mode}")
        if mode == "normal":
            sim.clear_faults()
        elif mode == "slow":
            sim.fault = "slow"
            sim.slow_seconds = _float(request, "seconds", 12)
        elif mode == "latency":
            # A profile, not a fault: composes with whatever fault is active.
            sim.set_latency(_float(request, "mean", 0.8), _float(request, "jitter", 0.4), _seed(request))
        elif mode == "busy-burst":
            sim.fault = "busy-burst"
            sim.burst_remaining = max(1, int(_float(request, "count", 3)))
        elif mode == "flaky":
            sim.fault = "flaky"
            sim.flaky = {
                "start": time.monotonic(),
                "down": _float(request, "down", 3),
                "up": _float(request, "up", 10),
                "cycles": max(1, int(_float(request, "cycles", 5))),
            }
        elif mode == "lossy":
            sim.fault = "lossy"
            sim.set_lossy(_float(request, "rate", 0.2), _seed(request))
        else:
            sim.fault = mode
        request["note"] = f"fault={sim.fault}" + (f" latency={sim.latency}" if mode == "latency" else "")
        return web.json_response(sim.status() | {"log": None})

    async def sim_reply_mode(request: web.Request) -> web.Response:
        mode = request.query.get("mode", "applied")
        if mode not in REPLY_MODES:
            raise web.HTTPBadRequest(text=f"mode must be one of {', '.join(REPLY_MODES)}")
        sim.reply_mode = mode
        sim.apply_delay = _float(request, "delay", sim.apply_delay)
        request["note"] = f"reply_mode={mode} delay={sim.apply_delay:g}s"
        return web.json_response({"reply_mode": sim.reply_mode, "apply_delay": sim.apply_delay})

    async def sim_validation(request: web.Request) -> web.Response:
        mode = request.query.get("mode", "reject")
        if mode not in VALIDATION_MODES:
            raise web.HTTPBadRequest(text=f"mode must be one of {', '.join(VALIDATION_MODES)}")
        sim.validation_mode = mode
        request["note"] = f"validation={mode}"
        return web.json_response({"validation_mode": sim.validation_mode})

    async def sim_ble(request: web.Request) -> web.Response:
        code = request.match_info["code"].upper()
        if code not in BLE_COMMANDS:
            raise web.HTTPBadRequest(text=f"BLE command must be one of {', '.join(BLE_COMMANDS)}")
        sim.ble_command(code)
        request["note"] = f"BLE {code}: tcpEnabled={sim.hub_settings['tcpEnabled']} http_up={sim.http_up}"
        return web.json_response({"tcp_enabled": sim.hub_settings["tcpEnabled"], "http_up": sim.http_up})

    async def sim_power_cycle(request: web.Request) -> web.Response:
        seconds = _float(request, "seconds", 5)
        sim.power_cycle(seconds)
        request["note"] = f"power cycle, back in {seconds:g}s"
        return web.json_response({"booting_seconds": seconds, "tcp_enabled": sim.hub_settings["tcpEnabled"]})

    async def sim_reboot(request: web.Request) -> web.Response:
        seconds = _float(request, "seconds", 5)
        firmware = {
            key: request.query[key] for key in ("firmware", "firmwareSpi", "firmwareWifi") if request.query.get(key)
        }
        sim.power_cycle(seconds, firmware)
        request["note"] = f"reboot, back in {seconds:g}s" + (f" with {firmware}" if firmware else "")
        return web.json_response({"booting_seconds": seconds, "firmware": firmware})

    async def sim_external(request: web.Request) -> web.Response:
        name = sim.external_change()
        request["origin"] = "app"
        request["note"] = f"external change -> {name}"
        return web.json_response({"state": sim.state})

    async def sim_profile(request: web.Request) -> web.Response:
        profile = request.match_info["profile"]
        if profile not in PROFILES:
            raise web.HTTPBadRequest(text=f"Unknown profile: {profile}")
        sim.state = dict(PROFILES[profile])
        request["note"] = f"profile={profile}"
        return web.json_response({"state": sim.state})

    async def sim_status(request: web.Request) -> web.Response:
        return web.json_response(sim.status())

    async def sim_panel(request: web.Request) -> web.Response:
        return web.Response(text=PANEL_HTML, content_type="text/html")

    app = web.Application(middlewares=[log_mw, device_mw, validate_mw])
    app.router.add_get(PATH_HUB_SETTINGS, get_hub_settings)
    app.router.add_get(PATH_CURRENTLY_PLAYING, get_currently_playing)
    for path in (PATH_PLAY, *PLAY_ALIASES):
        app.router.add_post(path, post_play)

    app.router.add_get("/_sim", sim_panel)
    app.router.add_get("/_sim/status", sim_status)
    app.router.add_post("/_sim/mode/{mode}", sim_mode)
    app.router.add_post("/_sim/play-reply-mode", sim_reply_mode)
    app.router.add_post("/_sim/validation", sim_validation)
    app.router.add_post("/_sim/ble/{code}", sim_ble)
    app.router.add_post("/_sim/power-cycle", sim_power_cycle)
    app.router.add_post("/_sim/reboot", sim_reboot)
    app.router.add_post("/_sim/external", sim_external)
    app.router.add_post("/_sim/profile/{profile}", sim_profile)
    return app


PANEL_HTML: Final = """<!doctype html>
<html><head><meta charset="utf-8"><title>Hub2 Simulator</title>
<style>
 :root { color-scheme: light dark;
   --bg:#fff; --fg:#1a1a1a; --mut:#666; --line:#e0e0e0; --card:#f7f7f7; }
 @media (prefers-color-scheme: dark) { :root {
   --bg:#16181c; --fg:#e8e8e8; --mut:#999; --line:#2c2f36; --card:#1e2127; } }
 body { font:14px/1.5 ui-sans-serif,system-ui,sans-serif;
   margin:0; padding:24px; background:var(--bg); color:var(--fg); }
 h1 { font-size:18px; margin:0 0 4px; }
 h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
   color:var(--mut); margin:24px 0 8px; }
 p.sub { color:var(--mut); margin:0 0 20px; }
 button { font:inherit; padding:6px 12px; margin:0 6px 6px 0; cursor:pointer;
   border:1px solid var(--line); border-radius:6px;
   background:var(--card); color:var(--fg); }
 button:hover { border-color:var(--mut); }
 #status { font-family:ui-monospace,monospace; padding:10px 12px;
   background:var(--card); border:1px solid var(--line);
   border-radius:6px; margin-bottom:8px; white-space:pre-wrap; }
 table { width:100%; border-collapse:collapse;
   font-family:ui-monospace,monospace; font-size:12px; }
 th { text-align:left; color:var(--mut); font-weight:500;
   border-bottom:1px solid var(--line); padding:6px 8px; }
 td { padding:4px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
 td.d { color:var(--mut); } .wrap { overflow-x:auto; }
</style></head><body>
<h1 id="title">Hub2 Simulator</h1>
<p class="sub">Fake Gemstone controller. Newest requests first;
Δ is the gap since the previous device request.</p>
<div id="status">loading…</div>
<h2>Scene</h2>
<div>
 <button onclick="post('/_sim/external')">external change (as app)</button>
 <button onclick="post('/_sim/profile/color-on')">colour on</button>
 <button onclick="post('/_sim/profile/pattern-playing')">pattern</button>
 <button onclick="post('/_sim/profile/architectural-playing')">architectural</button>
 <button onclick="post('/_sim/profile/playlist-playing')">playlist (assumed shape)</button>
 <button onclick="post('/_sim/profile/impulse-playing')">impulse (assumed shape)</button>
 <button onclick="post('/_sim/profile/off')">off</button>
</div>
<h2>Capabilities</h2>
<div>
 <button onclick="post('/_sim/ble/B800')">BLE B800 (disable HTTP; needs power cycle)</button>
 <button onclick="post('/_sim/power-cycle?seconds=5')">power cycle (5s)</button>
 <button onclick="post('/_sim/ble/B801')">BLE B801 (enable HTTP)</button>
 <button onclick="post('/_sim/reboot?seconds=5&firmware=1.2.0&firmwareSpi=1.1.0')">reboot → firmware 1.2.0</button>
</div>
<h2>Faults</h2>
<div>
 <button onclick="post('/_sim/mode/normal')">normal (clears all)</button>
 <button onclick="post('/_sim/mode/busy-once')">503 once</button>
 <button onclick="post('/_sim/mode/busy-burst?count=3')">503 burst x3</button>
 <button onclick="post('/_sim/mode/busy')">503 always</button>
 <button onclick="post('/_sim/mode/error')">500 always</button>
 <button onclick="post('/_sim/mode/unauthorized')">401 always</button>
 <button onclick="post('/_sim/mode/down')">offline</button>
</div>
<h2>Timing</h2>
<div>
 <button onclick="post('/_sim/mode/slow?seconds=2')">slow 2s</button>
 <button onclick="post('/_sim/mode/slow?seconds=9')">slow 9s</button>
 <button onclick="post('/_sim/mode/slow?seconds=12')">slow 12s (timeout)</button>
 <button onclick="post('/_sim/mode/latency?mean=0.8&jitter=0.4')">latency 0.8±0.4s</button>
 <button onclick="post('/_sim/mode/flaky?down=3&up=10&cycles=5')">flaky Wi-Fi 3s/10s x5</button>
 <button onclick="post('/_sim/mode/lossy?rate=0.2')">lossy 20%</button>
</div>
<h2>Out-of-range values (Q5)</h2>
<div>
 <button onclick="post('/_sim/validation?mode=reject')">reject with 400 (assumed)</button>
 <button onclick="post('/_sim/validation?mode=clamp')">clamp into bounds</button>
</div>
<h2>Play reply reflects</h2>
<div>
 <button onclick="post('/_sim/play-reply-mode?mode=applied')">applied state</button>
 <button onclick="post('/_sim/play-reply-mode?mode=accepted&delay=8')">accepted only (8s lag)</button>
 <button onclick="post('/_sim/play-reply-mode?mode=on-state-only')">onState-only replies (doc 3.4)</button>
</div>
<h2>Requests</h2>
<div class="wrap"><table><thead><tr>
<th>time</th><th>Δ</th><th>method</th><th>path</th>
<th>status</th><th>origin</th><th>note</th>
</tr></thead><tbody id="log"></tbody></table></div>
<script>
async function post(u){ await fetch(u,{method:'POST'}); refresh(); }
function cell(v, dim){ return `<td${dim?' class="d"':''}>${v}</td>`; }
async function refresh(){
  let r; try { r = await (await fetch('/_sim/status')).json(); } catch(e){ return; }
  document.getElementById('title').textContent = 'Hub2 Simulator — ' + r.name;
  const slow = r.fault==='slow' ? ' ('+r.slow_seconds+'s)' : '';
  const lat = r.latency ? `   latency=${r.latency.mean}±${r.latency.jitter}s` : '';
  const lag = r.reply_mode==='accepted' ? ' (+'+r.apply_delay+'s)' : '';
  const power = r.booting_seconds_left>0 ? `booting (${r.booting_seconds_left}s)`
    : (r.http_up ? 'serving' : 'HTTP disabled');
  const hs = r.hub_settings;
  document.getElementById('status').textContent =
    `device=${power}   tcpEnabled=${r.tcp_enabled}   `
    + `firmware=${hs.firmware} spi=${hs.firmwareSpi} wifi=${hs.firmwareWifi}\\n`
    + `outputs=${JSON.stringify(hs.pixelCount)} ${JSON.stringify(hs.pixelOutputNames)}\\n`
    + `fault=${r.fault}${slow}${lat}   validation=${r.validation_mode}   reply=${r.reply_mode}${lag}\\n`
    + `scene=${JSON.stringify(r.state)}`;
  document.getElementById('log').innerHTML = r.log.slice().reverse().map(e =>
    '<tr>'
    + cell(e.time, true)
    + cell(e.delta!==null ? 'Δ'+e.delta.toFixed(2)+'s' : '', true)
    + cell(e.method) + cell(e.path) + cell(e.status||'')
    + cell(e.origin, true) + cell(e.note, true)
    + '</tr>'
  ).join('');
}
refresh(); setInterval(refresh, 1000);
</script></body></html>
"""


# --- SDDP: the controller's side of Control4 discovery ----------------------

SDDP_GROUP: Final = "239.255.255.250"
SDDP_PORT: Final = 1902
SDDP_ALIVE: Final = "NOTIFY ALIVE SDDP/1.0"
SDDP_OFFLINE: Final = "NOTIFY OFFLINE SDDP/1.0"
SDDP_OK: Final = "SDDP/1.0 200 OK"

# Firmware fields, in firmware order (sddp_api.c). Max-Age is the seconds a
# Control4 Director keeps a device without hearing from it again.
SDDP_CONSTANT_FIELDS: Final = (
    "Max-Age: 1800",
    'Type: "GemstoneLights:controller"',
    'Proxies: "light"',
    'Primary-Proxy: "light"',
    'Manufacturer: "Gemstone Lights"',
    'Model: "Gemstone Lights"',
    'Driver: "gemstone_lights.c4z"',
)

_SDDP_FROM = re.compile(r'^From:\s*"([^"]*)"', re.MULTILINE)
_SDDP_TRAN = re.compile(r"^Tran:\s*(\d+)", re.MULTILINE)
_SDDP_TIMEOUT = re.compile(r"^Timeout:\s*(\d+)", re.MULTILINE)


def sddp_message(
    statement: str, serial: str, local_ip: str, *, tran: int | None = None, http_port: int | None = None
) -> bytes:
    """Render one SDDP response or NOTIFY as the firmware does, byte for byte.

    A search response carries the request's ``Tran``; a NOTIFY does not. The
    ``Http-Port`` line is this simulator's one extension and is only added for
    a port other than 80, so a simulator on 80 is indistinguishable from
    firmware.
    """
    lines = [statement, f'From: "{local_ip}:{SDDP_PORT}"', f'Host: "Gemstone-{serial}"']
    if tran is not None:
        lines.append(f"Tran: {tran}")
    lines.extend(SDDP_CONSTANT_FIELDS)
    if http_port is not None and http_port != 80:
        lines.append(f"Http-Port: {http_port}")
    return ("\r\n".join(lines) + "\r\n").encode()


def parse_search(data: bytes) -> tuple[str, int] | None:
    """Read a SEARCH the way the firmware does: From (quoted), Tran and Timeout, or nothing.

    Returns the address to answer and the transaction id to echo. The
    firmware's parser (``SDDPHandleSearchRequest``) drops a request missing
    any of the three, which is why a SEARCH from an off-the-shelf SDDP client
    gets no answer from a real controller either.
    """
    text = data.decode("utf-8", errors="replace")
    if not text.startswith("SEARCH"):
        return None
    from_match = _SDDP_FROM.search(text)
    tran_match = _SDDP_TRAN.search(text)
    if from_match is None or tran_match is None or _SDDP_TIMEOUT.search(text) is None:
        return None
    return from_match.group(1), int(tran_match.group(1))


class SddpResponder(asyncio.DatagramProtocol):
    """Answer searches and send NOTIFYs for every simulator in this process."""

    def __init__(self, sims: list[Simulator], local_ip: str) -> None:
        """Advertise from ``local_ip``, the address the HTTP servers are bound to."""
        self.sims = sims
        self.local_ip = local_ip
        self.transport: asyncio.DatagramTransport | None = None
        self.searches = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Keep the transport for replies."""
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Reply to the address in ``From`` at the port the search came from, as firmware does."""
        parsed = parse_search(data)
        if parsed is None or self.transport is None:
            return
        self.searches += 1
        from_ip, tran = parsed
        for sim in self.sims:
            # Nothing answers while a simulator is power-cycling.
            if sim.is_booting():
                continue
            message = sddp_message(SDDP_OK, sim.serial, self.local_ip, tran=tran, http_port=sim.http_port)
            self.transport.sendto(message, (from_ip, addr[1]))

    def notify(self, statement: str) -> None:
        """Multicast one NOTIFY per running simulator."""
        if self.transport is None:
            return
        for sim in self.sims:
            if sim.is_booting():
                continue
            message = sddp_message(statement, sim.serial, self.local_ip, http_port=sim.http_port)
            try:
                self.transport.sendto(message, (SDDP_GROUP, SDDP_PORT))
            except OSError:
                pass


async def start_sddp(
    sims: list[Simulator], local_ip: str, port: int
) -> tuple[asyncio.DatagramTransport, SddpResponder]:
    """Bind the SDDP socket (shared, so a real Director or the integration can coexist) and join the group."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT") and sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", port))
    # The default interface and the bound one; loopback may refuse, harmlessly.
    for member in dict.fromkeys(("0.0.0.0", local_ip)):
        membership = socket.inet_aton(SDDP_GROUP) + socket.inet_aton(member)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        except OSError:
            pass
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(lambda: SddpResponder(sims, local_ip), sock=sock)
    return transport, protocol


def parse_pixel_count(value: str) -> list[int]:
    """Turn ``104,52`` into the four-slot list the firmware reports, one per output."""
    try:
        counts = [int(part) for part in value.split(",") if part.strip()]
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"not a comma-separated list of integers: {value!r}") from err
    if not 1 <= len(counts) <= OUTPUTS or any(count < 0 for count in counts):
        raise argparse.ArgumentTypeError(f"expected 1-{OUTPUTS} non-negative integers, e.g. 104,52,0,0")
    return counts + [0] * (OUTPUTS - len(counts))


def parse_output_names(value: str) -> list[str]:
    """Turn ``Front roofline,Garage`` into the four-slot list of output names."""
    names = [part.strip() for part in value.split(",")]
    if len(names) > OUTPUTS:
        raise argparse.ArgumentTypeError(f"expected at most {OUTPUTS} names")
    return names + [""] * (OUTPUTS - len(names))


def parse_controller(value: str) -> tuple[str, int]:
    """Turn ``Garage:8081`` into a (name, port) pair."""
    name, sep, port = value.rpartition(":")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(f"expected NAME:PORT, got {value!r}")
    try:
        number = int(port)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"port must be an integer in {value!r}") from err
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range in {value!r}")
    return name.strip(), number


async def serve(controllers: list[tuple[str, int]], args: argparse.Namespace) -> None:
    """Run one controller per port on a single event loop until interrupted."""
    runners: list[web.AppRunner] = []
    sims: list[Simulator] = []
    sddp: tuple[asyncio.DatagramTransport, SddpResponder] | None = None
    advertiser: asyncio.Task[None] | None = None
    try:
        for name, port in controllers:
            sim = Simulator(args.profile, name=name, serial=f"SIM{port}")
            sim.http_port = port
            sim.hub_settings["firmware"] = args.firmware
            sim.hub_settings["pixelCount"] = args.pixel_count
            sim.hub_settings["pixelOutputNames"] = args.output_names
            sim.hub_settings["localIp"] = args.host
            sims.append(sim)
            runner = web.AppRunner(build_app(sim))
            await runner.setup()
            runners.append(runner)
            try:
                await web.TCPSite(runner, host=args.host, port=port).start()
            except PermissionError:
                sys.exit(
                    f"\nerror: binding port {port} requires root.\n\n"
                    f"Use the default instead -- the config flow has a port field, so there\n"
                    f"is no reason to bind a privileged port:\n\n"
                    f"    {sys.executable} {SCRIPT_PATH}\n"
                )
            except OSError as err:
                sys.exit(f"\nerror: could not bind {args.host}:{port} -- {err}")

        if args.sddp_port:
            try:
                sddp = await start_sddp(sims, args.host, args.sddp_port)
            except OSError as err:
                print(f"warning: SDDP disabled, could not bind UDP port {args.sddp_port}: {err}")
            else:
                if args.sddp_alive_interval > 0:
                    advertiser = asyncio.create_task(_advertise(sddp[1], args.sddp_alive_interval))

        # Flushed explicitly: stdout is block-buffered when redirected to a
        # file, so an unflushed banner simply never appears in a log.
        for sim, (name, port) in zip(sims, controllers, strict=True):
            print(f"Hub2 simulator '{name}' on http://{args.host}:{port}  (profile: {args.profile})")
            print(f"  control panel: http://{args.host}:{port}/_sim")
            if sddp is not None:
                print(f"  SDDP: Gemstone-{sim.serial} on UDP {args.sddp_port}, HTTP at {args.host}:{port}")
        if sddp is not None and args.host.startswith("127."):
            print("SDDP note: bound to loopback, so a discovered simulator cannot be reached at the address")
            print("its reply comes from. Use --host <this machine's LAN address> to test discovery.")
        print(f"Add each to Home Assistant with host {args.host} and its port.\n", flush=True)
        await asyncio.Event().wait()
    finally:
        if advertiser is not None:
            advertiser.cancel()
        if sddp is not None:
            sddp[1].notify(SDDP_OFFLINE)
            sddp[0].close()
        for runner in runners:
            await runner.cleanup()


async def _advertise(responder: SddpResponder, interval: float) -> None:
    """Send NOTIFY ALIVE now and every ``interval`` seconds, as firmware does every 300."""
    while True:
        responder.notify(SDDP_ALIVE)
        await asyncio.sleep(interval)


def main() -> None:
    """Parse arguments and run the server(s)."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="bind port (default: 8080, needs no privileges)")
    parser.add_argument("--name", default=HUB_SETTINGS["bluetoothName"], help="reported Bluetooth name")
    parser.add_argument(
        "--controller",
        action="append",
        type=parse_controller,
        metavar="NAME:PORT",
        help="run one controller per flag (repeatable); overrides --name and --port",
    )
    parser.add_argument("--profile", default="color-on", choices=sorted(PROFILES), help="initial scene")
    parser.add_argument("--firmware", default=HUB_SETTINGS["firmware"], help="reported firmware version")
    parser.add_argument(
        "--pixel-count",
        type=parse_pixel_count,
        default=",".join(str(count) for count in HUB_SETTINGS["pixelCount"]),
        help="reported pixel count per output, comma-separated (default: %(default)s)",
    )
    parser.add_argument(
        "--output-names",
        type=parse_output_names,
        default=",".join(HUB_SETTINGS["pixelOutputNames"]),
        help="reported output names, comma-separated (default: %(default)s)",
    )
    parser.add_argument(
        "--sddp-port",
        type=int,
        default=SDDP_PORT,
        help="UDP port to answer SDDP searches on; 0 disables SDDP (default: %(default)s)",
    )
    parser.add_argument(
        "--sddp-alive-interval",
        type=float,
        default=300.0,
        help="seconds between NOTIFY ALIVE multicasts; 0 disables them (default: %(default)s, as firmware)",
    )
    args = parser.parse_args()

    controllers = args.controller or [(args.name, args.port)]
    try:
        asyncio.run(serve(controllers, args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
