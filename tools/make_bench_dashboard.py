#!/usr/bin/env python3
"""Generate a Home Assistant dashboard that exposes every capability as a button.

Driving this integration by hand means either hunting through the light card's
dialogs or hand-writing YAML in Developer Tools, and neither shows you what the
device can actually do. This builds a single page where every colour, every
brightness step, all the animations and every simulator fault is one click, next
to a readout of what Home Assistant currently believes.

The effect buttons are generated from `const.ANIMATIONS` rather than written out,
so the page cannot drift from the integration. Entity ids are read from the
entity registry rather than assumed, because they derive from the controller's
reported name -- point the simulator at a different `bluetoothName` and every
entity id changes with it.

Run by `tools/dev_ha.sh` before Home Assistant starts. Safe to re-run: the
dashboard and the managed block at the end of configuration.yaml are rewritten
every time, so a new button always has its rest_command behind it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "ha_config"
DASHBOARD = CONFIG_DIR / "gemstone_bench.yaml"
CONFIGURATION = CONFIG_DIR / "configuration.yaml"

sys.path.insert(0, str(REPO))
from custom_components.gemstone_lights.const import ANIMATIONS  # noqa: E402

FALLBACK_ENTITY = "light.front_roofline"
SIM = "http://127.0.0.1:8080"

SENTINEL = "# --- gemstone test bench (managed by tools/make_bench_dashboard.py) ---"

# Chosen to prove the wire encoding rather than to look nice: red is 255 in this
# packing, not 16711680, and the warm channel is separate from RGB.
COLORS: list[tuple[str, list[int], str]] = [
    ("Red", [255, 0, 0, 0], "mdi:circle"),
    ("Green", [0, 255, 0, 0], "mdi:circle"),
    ("Blue", [0, 0, 255, 0], "mdi:circle"),
    ("Warm white", [0, 0, 0, 255], "mdi:circle-outline"),
    ("Gemstone blue", [35, 123, 192, 0], "mdi:circle"),
    ("All channels", [255, 255, 255, 255], "mdi:circle-multiple"),
]

BRIGHTNESS: list[tuple[str, int]] = [("1%", 3), ("25%", 64), ("50%", 128), ("100%", 255)]

# name -> (rest_command suffix, path, icon, note)
FAULTS: list[tuple[str, str, str, str]] = [
    ("Normal", "normal", "mdi:check-circle", "/_sim/mode/normal"),
    ("503 once", "busy_once", "mdi:reload-alert", "/_sim/mode/busy-once"),
    ("503 always", "busy", "mdi:alert-octagon", "/_sim/mode/busy"),
    ("Slow 2s", "slow_2", "mdi:timer-sand", "/_sim/mode/slow?seconds=2"),
    ("Slow 9s", "slow_9", "mdi:timer-sand", "/_sim/mode/slow?seconds=9"),
    ("Slow 12s", "slow_12", "mdi:timer-alert", "/_sim/mode/slow?seconds=12"),
    ("Offline", "down", "mdi:lan-disconnect", "/_sim/mode/down"),
    ("500 always", "error", "mdi:alert", "/_sim/mode/error"),
    ("401 always", "unauthorized", "mdi:lock-alert", "/_sim/mode/unauthorized"),
]

SCENES: list[tuple[str, str, str, str]] = [
    ("External change", "external", "mdi:cellphone-wireless", "/_sim/external"),
    ("Seed: colour", "profile_color", "mdi:palette", "/_sim/profile/color-on"),
    ("Seed: pattern", "profile_pattern", "mdi:animation-play", "/_sim/profile/pattern-playing"),
    (
        "Seed: architectural",
        "profile_architectural",
        "mdi:home-lightbulb-outline",
        "/_sim/profile/architectural-playing",
    ),
    ("Seed: off", "profile_off", "mdi:power-off", "/_sim/profile/off"),
    ("Seed: playlist", "profile_playlist", "mdi:playlist-play", "/_sim/profile/playlist-playing"),
    ("Seed: impulse", "profile_impulse", "mdi:flash", "/_sim/profile/impulse-playing"),
]

REPLY: list[tuple[str, str, str, str]] = [
    ("Reply = applied", "reply_applied", "mdi:check-decagram", "/_sim/play-reply-mode?mode=applied"),
    ("Reply = accepted", "reply_accepted", "mdi:clock-alert", "/_sim/play-reply-mode?mode=accepted&delay=8"),
    ("Reply = onState only", "reply_on_state_only", "mdi:power-settings", "/_sim/play-reply-mode?mode=on-state-only"),
]

# The controller's documented capabilities beyond the scene: the BLE enable /
# disable of the HTTP server with the power cycle the document requires, and a
# reboot that comes back on new firmware.
CAPABILITIES: list[tuple[str, str, str, str]] = [
    ("BLE B800: disable HTTP", "ble_b800", "mdi:bluetooth-off", "/_sim/ble/B800"),
    ("Power cycle 5s", "power_cycle", "mdi:power-cycle", "/_sim/power-cycle?seconds=5"),
    ("BLE B801: enable HTTP", "ble_b801", "mdi:bluetooth", "/_sim/ble/B801"),
    ("Reboot to firmware 1.2.0", "reboot_120", "mdi:update", "/_sim/reboot?seconds=5&firmware=1.2.0&firmwareSpi=1.1.0"),
]

# Timing and failure profiles: what a real Wi-Fi link does to a poll loop.
TIMING: list[tuple[str, str, str, str]] = [
    ("Latency 0.8s +/- 0.4", "latency", "mdi:timer-outline", "/_sim/mode/latency?mean=0.8&jitter=0.4"),
    ("Flaky 3s down / 10s up x5", "flaky", "mdi:wifi-strength-1-alert", "/_sim/mode/flaky?down=3&up=10&cycles=5"),
    ("Lossy 20%", "lossy", "mdi:wifi-strength-2", "/_sim/mode/lossy?rate=0.2"),
    ("503 burst x3", "busy_burst", "mdi:reload-alert", "/_sim/mode/busy-burst?count=3"),
]

# The two readings of what the firmware does with an out-of-range value (Q5).
VALIDATION: list[tuple[str, str, str, str]] = [
    ("Reject with 400", "validate_reject", "mdi:close-octagon", "/_sim/validation?mode=reject"),
    ("Clamp into bounds", "validate_clamp", "mdi:arrow-collapse", "/_sim/validation?mode=clamp"),
]

# The integration's own actions, with payloads that show every field on the
# wire. Saving keeps whatever is playing in the Pattern control, which is
# how a pattern built in the app is kept.
ACTIONS: list[tuple[str, str, str, dict[str, Any]]] = [
    (
        "Play pattern (action)",
        "mdi:animation-play",
        "gemstone_lights.play_pattern",
        {
            "animation": "chase",
            "colors": [[255, 0, 0, 0], [0, 255, 0, 0], [0, 0, 255, 0]],
            "brightness": 200,
            "speed": 200,
            "direction": 1,
            "background_color": [0, 0, 0, 30],
            "name": "Bench pattern",
        },
    ),
    (
        "Save what is playing",
        "mdi:content-save-outline",
        "gemstone_lights.save_playing",
        {"name": "Bench save"},
    ),
]


def registry_entities() -> list[dict[str, Any]]:
    """Every entity this integration has registered, or [] before the first setup."""
    registry = CONFIG_DIR / ".storage" / "core.entity_registry"
    if not registry.exists():
        return []
    try:
        data = json.loads(registry.read_text())
    except (OSError, ValueError):
        return []
    return [
        entity for entity in data.get("data", {}).get("entities", []) if entity.get("platform") == "gemstone_lights"
    ]


def find_entity_id() -> str:
    """Read the light's entity id from the registry, or fall back.

    Filtered to the light: the registry also holds the integration's number and
    sensor entities, and the first entry is not necessarily the light.
    """
    for entity in registry_entities():
        if str(entity["entity_id"]).startswith("light."):
            return str(entity["entity_id"])
    return FALLBACK_ENTITY


def companion_entity_ids() -> list[str]:
    """List the enabled non-light entities: the effect controls and diagnostics."""
    return sorted(
        str(entity["entity_id"])
        for entity in registry_entities()
        if not str(entity["entity_id"]).startswith("light.") and not entity.get("disabled_by")
    )


def action_button(name: str, icon: str, action: str, entity: str, data: dict[str, Any]) -> dict[str, Any]:
    """Build a button that performs one action on the light with fixed data."""
    return {
        "type": "button",
        "name": name,
        "icon": icon,
        "show_state": False,
        "tap_action": {
            "action": "perform-action",
            "perform_action": action,
            "target": {"entity_id": entity},
            "data": data,
        },
    }


def light_button(name: str, icon: str, entity: str, data: dict[str, Any]) -> dict[str, Any]:
    """Build a button that calls light.turn_on with fixed parameters."""
    return action_button(name, icon, "light.turn_on", entity, data)


def command_button(name: str, suffix: str, icon: str) -> dict[str, Any]:
    """Build a button that pokes the simulator's control plane."""
    return {
        "type": "button",
        "name": name,
        "icon": icon,
        "tap_action": {"action": "perform-action", "perform_action": f"rest_command.gemstone_sim_{suffix}"},
    }


def grid(cards: list[dict[str, Any]], columns: int = 3) -> dict[str, Any]:
    """Lay buttons out in a grid that does not force square tiles."""
    return {"type": "grid", "columns": columns, "square": False, "cards": cards}


def heading(text: str) -> dict[str, Any]:
    """Render a markdown card used as a section heading."""
    return {"type": "markdown", "content": text}


def build_dashboard(entity: str, companions: list[str]) -> dict[str, Any]:
    """Assemble the whole page."""
    companion_card: dict[str, Any] = (
        {"type": "entities", "title": "Effect parameters and diagnostics", "entities": companions}
        if companions
        else heading("_The effect controls and diagnostic sensors appear here after the first setup._")
    )
    cards: list[dict[str, Any]] = [
        heading(
            "## Gemstone test bench\n"
            f"Driving `{entity}` against the simulator at {SIM}.\n\n"
            f"Open the [simulator log]({SIM}/_sim) in another tab to watch requests arrive. "
            "Commands from Home Assistant are tagged `origin: homeassistant`; "
            "the *External change* button below is tagged `app`."
        ),
        {"type": "light", "entity": entity},
        {
            "type": "entities",
            "title": "What Home Assistant believes",
            "entities": [
                {"entity": entity, "name": "State"},
                {"type": "attribute", "entity": entity, "attribute": "brightness", "name": "Brightness"},
                {"type": "attribute", "entity": entity, "attribute": "rgbw_color", "name": "RGBW"},
                {"type": "attribute", "entity": entity, "attribute": "effect", "name": "Effect"},
                {"type": "attribute", "entity": entity, "attribute": "playing", "name": "Playing"},
                {"type": "attribute", "entity": entity, "attribute": "playing_name", "name": "Playing name"},
                {"type": "attribute", "entity": entity, "attribute": "color_mode", "name": "Colour mode"},
            ],
        },
        companion_card,
        heading("### Power"),
        grid(
            [
                {
                    "type": "button",
                    "name": "Turn on",
                    "icon": "mdi:lightbulb-on",
                    "tap_action": {
                        "action": "perform-action",
                        "perform_action": "light.turn_on",
                        "target": {"entity_id": entity},
                    },
                },
                {
                    "type": "button",
                    "name": "Turn off",
                    "icon": "mdi:lightbulb-off",
                    "tap_action": {
                        "action": "perform-action",
                        "perform_action": "light.turn_off",
                        "target": {"entity_id": entity},
                    },
                },
                {
                    "type": "button",
                    "name": "Toggle",
                    "icon": "mdi:light-switch",
                    "tap_action": {
                        "action": "perform-action",
                        "perform_action": "light.toggle",
                        "target": {"entity_id": entity},
                    },
                },
            ]
        ),
        heading(
            "### Colours\n"
            "Watch the wire value in the simulator log: pure red is **255**, not 16711680. "
            "Red occupies the lowest byte in this encoding."
        ),
        grid([light_button(n, i, entity, {"rgbw_color": c, "brightness": 200}) for n, c, i in COLORS]),
        heading(
            "### Brightness\n"
            "With a pattern or an architectural design running, brightness alone replays it "
            "instead of collapsing it to a static colour. Pick an animation below, or press "
            "*Seed: architectural*, then press these."
        ),
        grid([light_button(n, "mdi:brightness-6", entity, {"brightness": b}) for n, b in BRIGHTNESS], columns=4),
        heading(
            f"### Animations ({len(ANIMATIONS)})\n"
            "Every animation the integration offers, sent the way Home Assistant's own effect picker sends them."
        ),
        grid(
            [light_button(display, "mdi:animation", entity, {"effect": display}) for display in ANIMATIONS.values()],
            columns=4,
        ),
        heading(
            "### Actions\n"
            "The integration's own actions with sample payloads: watch the simulator log for "
            "the `pattern` and `architectural` shapes. The third button asks for pixel 999 and "
            "must be refused with a red toast before anything reaches the simulator."
        ),
        grid([action_button(n, i, a, entity, d) for n, i, a, d in ACTIONS], columns=3),
        heading(
            "### Simulator faults\n"
            "These reach the fake controller, not Home Assistant. Set the idle poll "
            "interval to 5s under the integration's **Configure** first, or the backoff "
            "ladder is only 60 → 120 and you cannot tell the cap from the second step."
        ),
        grid([command_button(n, s, i) for n, s, i, _ in FAULTS], columns=4),
        heading(
            "### Capabilities\n"
            "**BLE B800** flips the controller's TCP flag but, per the document, the server keeps "
            "serving until a **power cycle** -- then the light goes unavailable, indistinguishable "
            "from offline, until **BLE B801**. **Reboot** comes back on new firmware: watch the "
            "firmware sensors and the `hub-settings` re-read in the simulator log."
        ),
        grid([command_button(n, s, i) for n, s, i, _ in CAPABILITIES], columns=4),
        heading(
            "### Timing\n"
            "Latency composes with any fault; the others are faults of their own. Watch the Δ "
            "column: flaky shows the backoff ladder and recovery five times over without clicking, "
            "and a 503 burst of two is what the client's single retry cannot absorb."
        ),
        grid([command_button(n, s, i) for n, s, i, _ in TIMING], columns=4),
        heading(
            "### Out-of-range values (Q5)\n"
            "The document does not say whether the firmware rejects or clamps an out-of-range value. "
            "Switch to **clamp**, then send speed 999 from Developer Tools: the reply says 255."
        ),
        grid([command_button(n, s, i) for n, s, i, _ in VALIDATION], columns=2),
        heading("### Scene, from outside Home Assistant"),
        grid([command_button(n, s, i) for n, s, i, _ in SCENES], columns=4),
        heading(
            "### What does a play reply carry?\n"
            "**accepted**: the open question in the README. Press a colour: the light jumps "
            "to it, and the next poll drags it back because the device has not caught up yet. "
            "**onState only**: the document's section 3.4 shape, where a power toggle answers "
            "with nothing but `onState`. Press *Turn off* then *Turn on*: the card must keep "
            "its colour and brightness, because the coordinator carries the scene forward."
        ),
        grid([command_button(n, s, i) for n, s, i, _ in REPLY], columns=3),
    ]
    return {
        "title": "Gemstone Test Bench",
        "views": [{"title": "Bench", "path": "bench", "icon": "mdi:test-tube", "cards": cards}],
    }


def rest_commands() -> str:
    """Build the rest_command block that the fault buttons call."""
    commands: dict[str, Any] = {}
    for _, suffix, _, path in FAULTS + SCENES + REPLY + CAPABILITIES + TIMING + VALIDATION:
        commands[f"gemstone_sim_{suffix}"] = {"url": f"{SIM}{path}", "method": "post"}
    return yaml.safe_dump({"rest_command": commands}, sort_keys=False, default_flow_style=False)


def lovelace_block() -> str:
    """Register the generated dashboard. The url key must contain a hyphen."""
    return yaml.safe_dump(
        {
            "lovelace": {
                "mode": "storage",
                "dashboards": {
                    "gemstone-bench": {
                        "mode": "yaml",
                        "title": "Gemstone Test Bench",
                        "icon": "mdi:test-tube",
                        "show_in_sidebar": True,
                        "filename": "gemstone_bench.yaml",
                    }
                },
            }
        },
        sort_keys=False,
        default_flow_style=False,
    )


def main() -> None:
    """Write the dashboard and make sure configuration.yaml registers it."""
    if not CONFIG_DIR.is_dir():
        sys.exit(f"error: {CONFIG_DIR} does not exist -- run tools/dev_ha.sh first")

    entity = find_entity_id()
    companions = companion_entity_ids()
    DASHBOARD.write_text(yaml.safe_dump(build_dashboard(entity, companions), sort_keys=False, default_flow_style=False))
    print(f"wrote {DASHBOARD.relative_to(REPO)}  (entity: {entity}, companions: {len(companions)})")

    existing = CONFIGURATION.read_text() if CONFIGURATION.exists() else ""
    # The managed block is always the tail of the file, so replacing from the
    # sentinel onward rewrites it without touching anything hand-edited above.
    if SENTINEL in existing:
        existing = existing[: existing.index(SENTINEL)].rstrip("\n") + "\n"
    CONFIGURATION.write_text(f"{existing}\n{SENTINEL}\n\n{lovelace_block()}\n{rest_commands()}")
    count = len(FAULTS + SCENES + REPLY + CAPABILITIES + TIMING + VALIDATION)
    print(f"registered the dashboard and {count} rest commands in {CONFIGURATION.relative_to(REPO)}")


if __name__ == "__main__":
    main()
