"""Tests for the development simulator in `tools/hub2_sim.py`.

The point of these is to stop the simulator drifting into a fake that only
agrees with itself. A simulator is only worth running in a browser if its wire
shapes are the ones the real parsers accept, so everything it renders is fed
back through `parse_hub_settings` / `parse_currently_playing`, and its scene
shapes are checked against the fixtures in `payloads.py` -- the same fixtures
the component tests use, taken from the API documents.

The socket tests drive a real `GemstoneClient` over a real TCP connection, which
is the one thing the other tests cannot do: they mock at the session
boundary, so the middleware, the status codes and the client's retry loop are
never exercised against each other.
"""

import argparse
import asyncio
import json
import socket
import time
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.gemstone_lights import sddp
from custom_components.gemstone_lights.api import (
    GemstoneBusyError,
    GemstoneClient,
    GemstoneConnectionError,
    GemstoneResponseError,
    build_architectural_replay_command,
    build_color_command,
    build_on_state_command,
    build_pattern_command,
    build_pattern_replay_command,
    parse_currently_playing,
    parse_hub_settings,
)
from custom_components.gemstone_lights.const import DOMAIN, EFFECT_LIST
from custom_components.gemstone_lights.sddp import build_search_request, parse_sddp
from tools.hub2_sim import (
    DEFAULT_BLUE,
    HUB_SETTINGS,
    PROFILES,
    SDDP_ALIVE,
    SDDP_OK,
    Simulator,
    apply_command,
    build_app,
    clamp_command,
    parse_controller,
    parse_output_names,
    parse_search,
    reported,
    sddp_message,
    start_sddp,
    validate_command,
)

from .payloads import ARCHITECTURAL_RESPONSE, COLOR_ON_RESPONSE, HUB_SETTINGS_RESPONSE, PATTERN_RESPONSE
from .test_api import HUB_SETTINGS_RESPONSE as REAL_HUB_REPLY
from .test_sddp import FIRMWARE_ALIVE, FIRMWARE_RESPONSE


@pytest.fixture
def sim() -> Simulator:
    """Build a simulator seeded with a colour scene."""
    return Simulator("color-on")


@pytest.fixture
async def client(sim: Simulator, socket_enabled: None) -> Any:
    """Wire a real `GemstoneClient` to the simulator over a real socket."""
    server = TestServer(build_app(sim))
    await server.start_server()
    test_client = TestClient(server)
    session = aiohttp.ClientSession()
    gemstone = GemstoneClient("127.0.0.1", session, port=server.port)
    try:
        yield gemstone, test_client, sim
    finally:
        # The harness fails any test that leaves a task or timer behind, so a
        # scheduled convergence and both sessions have to go explicitly.
        sim.cancel_pending()
        await session.close()
        await test_client.close()


# --- Shapes the real parsers must accept ------------------------------------


def test_hub_settings_parses_and_matches_the_documented_fixture(sim: Simulator) -> None:
    rendered = reported({"hubSettings": sim.hub_settings})
    settings = parse_hub_settings(rendered)

    documented = parse_hub_settings(HUB_SETTINGS_RESPONSE)
    assert settings.firmware == documented.firmware
    assert settings.pixel_count == documented.pixel_count
    assert settings.tcp_enabled is True
    # config_flow titles the entry from this, so an empty one would silently
    # degrade the entry name to the bare host.
    assert settings.bluetooth_name


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_every_profile_parses(profile: str) -> None:
    playing = parse_currently_playing(reported({"currentlyPlaying": PROFILES[profile]}))
    assert playing.on_state is PROFILES[profile]["onState"]


def test_colour_profile_matches_the_documented_colour_shape() -> None:
    rendered = parse_currently_playing(reported({"currentlyPlaying": PROFILES["color-on"]}))
    documented = parse_currently_playing(COLOR_ON_RESPONSE)
    # Both must land in the colorB branch: hue full-scale, brightness separate.
    assert rendered.rgbw is not None and documented.rgbw is not None
    assert rendered.pattern is None and documented.pattern is None
    assert rendered.brightness == PROFILES["color-on"]["colorB"]["brightness"]


def test_pattern_profile_matches_the_documented_pattern_shape() -> None:
    rendered = parse_currently_playing(reported({"currentlyPlaying": PROFILES["pattern-playing"]}))
    documented = parse_currently_playing(PATTERN_RESPONSE)
    assert rendered.animation == documented.animation == "isofade"
    assert rendered.pattern is not None
    # The replay branch in light.py re-sends this object verbatim, so every field
    # the API requires has to survive a round trip.
    assert set(rendered.pattern) >= set(documented.pattern or {})


def test_architectural_profile_matches_the_documented_shape() -> None:
    rendered = parse_currently_playing(reported({"currentlyPlaying": PROFILES["architectural-playing"]}))
    documented = parse_currently_playing(ARCHITECTURAL_RESPONSE)
    assert rendered.scene == documented.scene == "architectural"
    assert rendered.architectural is not None and documented.architectural is not None
    # The replay branch re-sends this object verbatim, so its keys must match.
    assert set(rendered.architectural) == set(documented.architectural)
    assert rendered.rgbw == documented.rgbw


def test_hub_settings_reports_two_outputs() -> None:
    """The second output is what proves per-output handling downstream."""
    assert HUB_SETTINGS["pixelCount"] == [104, 52, 0, 0]
    assert parse_hub_settings(reported({"hubSettings": HUB_SETTINGS})).pixel_count == (104, 52, 0, 0)


def test_origin_is_echoed_and_defaults_to_http() -> None:
    assert reported({})["state"]["reported"]["origin"] == "http"
    assert reported({}, origin="homeassistant")["state"]["reported"]["origin"] == "homeassistant"


# --- Merge semantics --------------------------------------------------------


def test_power_toggle_preserves_the_running_scene() -> None:
    """A bare onState command must not blank the colour, per COLOR_OFF_RESPONSE."""
    off = apply_command(PROFILES["color-on"], {"onState": False})
    assert off["onState"] is False
    assert off["colorB"] == {"value": DEFAULT_BLUE, "brightness": 180}

    playing = parse_currently_playing(reported({"currentlyPlaying": off}))
    assert playing.on_state is False
    assert playing.rgbw is not None
    assert playing.brightness == 180


def test_power_toggle_preserves_a_running_pattern() -> None:
    off = apply_command(PROFILES["pattern-playing"], {"onState": False})
    assert off["pattern"] is not None
    assert parse_currently_playing(reported({"currentlyPlaying": off})).animation == "isofade"


def test_a_colour_command_clears_a_running_pattern() -> None:
    command = {
        "color": None,
        "colorB": {"value": 255, "brightness": 64},
        "pattern": None,
        "architectural": None,
        "onState": True,
    }
    updated = apply_command(PROFILES["pattern-playing"], command)
    assert updated["pattern"] is None
    playing = parse_currently_playing(reported({"currentlyPlaying": updated}))
    assert playing.animation is None
    # Pure red is 255 in this encoding, not 16711680.
    assert playing.rgbw == (255, 0, 0, 0)


# --- Validation: the contract between the client's builders and the fake ----


@pytest.mark.parametrize(
    "command",
    [
        build_on_state_command(False),
        build_color_command((255, 0, 0, 0), 128),
        build_pattern_command("chase", "x" * 32, [(255, 0, 0, 0)] * 20, 255, speed=255, direction=5),
        # An animation that takes no direction and does carry extra parameters.
        build_pattern_command("motionless", "Warm", [(255, 0, 0, 0)], 180),
        build_pattern_replay_command(PROFILES["pattern-playing"]["pattern"], 100, speed=5),
        build_architectural_replay_command(PROFILES["architectural-playing"]["architectural"], 50),
    ],
    ids=["on-state", "colour", "pattern", "pattern-no-direction", "pattern-replay", "architectural-replay"],
)
def test_validate_command_accepts_every_builder_output(command: dict[str, Any]) -> None:
    """If a builder emits something the simulator rejects, one of them is wrong."""
    assert validate_command(command) is None


@pytest.mark.parametrize(
    ("command", "fragment"),
    [
        ({"pattern": {**PROFILES["pattern-playing"]["pattern"], "direction": 11}}, "direction"),
        ({"pattern": {**PROFILES["pattern-playing"]["pattern"], "name": ""}}, "name"),
        ({"pattern": {**PROFILES["pattern-playing"]["pattern"], "colors": [255] * 21}}, "colors"),
        ({"pattern": {**PROFILES["pattern-playing"]["pattern"], "speed": True}}, "speed"),
        ({"architectural": {"name": "x", "brightness": 255, "staticColors": []}}, "staticColors"),
        ({"architectural": {"name": "x", "staticColors": [{"lights": [-1], "color": 1}]}}, "lights"),
        ({"colorB": {"value": 255, "brightness": 300}}, "brightness"),
        ({"colorB": {"value": 255, "brightness": 1}, "pattern": {"name": "x"}}, "Exactly one"),
        ({"onState": "yes"}, "onState"),
    ],
    ids=[
        "direction",
        "name",
        "colours",
        "bool-speed",
        "no-segments",
        "negative-index",
        "brightness",
        "two-scenes",
        "on",
    ],
)
def test_validate_command_rejects_out_of_range_values(command: dict[str, Any], fragment: str) -> None:
    reason = validate_command(command)
    assert reason is not None
    assert fragment in reason


def test_external_change_moves_the_scene() -> None:
    sim = Simulator("color-on")
    before = sim.state["colorB"]["value"]
    sim.external_change()
    assert sim.state["colorB"]["value"] != before
    assert sim.state["onState"] is True


# --- The real client over a real socket -------------------------------------


async def test_client_reads_settings_and_state(client: Any) -> None:
    gemstone, _, _ = client
    settings = await gemstone.async_get_hub_settings()
    assert settings.firmware == HUB_SETTINGS["firmware"]

    playing = await gemstone.async_get_currently_playing()
    assert playing.on_state is True
    assert playing.brightness == 180


async def test_client_round_trips_a_colour(client: Any) -> None:
    gemstone, _, _ = client
    result = await gemstone.async_play_color((255, 0, 0, 0), 200)
    assert result.rgbw == (255, 0, 0, 0)
    assert result.brightness == 200
    # And the simulator now reports it, so a poll agrees with the command reply.
    assert (await gemstone.async_get_currently_playing()).rgbw == (255, 0, 0, 0)


async def test_client_round_trips_a_pattern_then_powers_off(client: Any) -> None:
    gemstone, _, _ = client
    result = await gemstone.async_play_pattern("isofade", "IsoFade", [(255, 0, 0, 0)], 128)
    assert result.animation == "isofade"

    off = await gemstone.async_set_on_state(False)
    assert off.on_state is False
    assert off.animation == "isofade"


async def test_the_simulator_rejects_out_of_range_values_with_400(client: Any) -> None:
    """The assumed-400 path reaches the client as a response error, not a silent clamp."""
    gemstone, _, sim = client
    before = dict(sim.state)
    with pytest.raises(GemstoneResponseError) as err:
        await gemstone.async_play({"pattern": {**PROFILES["pattern-playing"]["pattern"], "direction": 11}})
    assert err.value.status == 400
    assert sim.state == before


async def test_on_state_only_reply_mode_returns_bare_power(client: Any) -> None:
    """Section 3.4 literally: a power toggle's reply carries nothing but onState."""
    gemstone, test_client, sim = client
    await test_client.post("/_sim/play-reply-mode?mode=on-state-only")

    reply = await gemstone.async_set_on_state(False)
    assert reply.raw == {"onState": False}
    assert reply.scene is None
    assert reply.on_state is False

    # The device did apply it; a poll shows the same scene, powered off.
    polled = await gemstone.async_get_currently_playing()
    assert polled.on_state is False
    assert polled.scene == "color"
    assert sim.state["colorB"]["value"] == DEFAULT_BLUE

    # A scene command still replies with the whole scene in this mode.
    colour = await gemstone.async_play_color((255, 0, 0, 0), 200)
    assert colour.scene == "color"


async def test_a_single_503_is_retried_transparently(client: Any) -> None:
    gemstone, test_client, _ = client
    await test_client.post("/_sim/mode/busy-once")
    # No exception: api.py retries once after 0.5s, which is invisible without
    # the DEBUG log line.
    assert (await gemstone.async_get_currently_playing()).on_state is True


async def test_a_persistent_503_surfaces_as_busy(client: Any) -> None:
    gemstone, test_client, _ = client
    await test_client.post("/_sim/mode/busy")
    with pytest.raises(GemstoneBusyError):
        await gemstone.async_get_currently_playing()


async def test_an_aborted_connection_surfaces_as_a_connection_error(client: Any) -> None:
    gemstone, test_client, _ = client
    await test_client.post("/_sim/mode/down")
    with pytest.raises(GemstoneConnectionError):
        await gemstone.async_get_currently_playing()


async def test_faults_can_be_cleared_while_a_fault_is_active(client: Any) -> None:
    """The control plane must stay reachable, or the only way out is `pkill`."""
    gemstone, test_client, _ = client
    await test_client.post("/_sim/mode/busy")
    assert (await test_client.post("/_sim/mode/normal")).status == 200
    assert (await gemstone.async_get_currently_playing()).on_state is True


async def test_the_simulator_rejects_what_the_firmware_rejects(client: Any) -> None:
    """A fake more permissive than the device is how a bug ships."""
    _, test_client, _ = client
    assert (await test_client.get("/nope")).status == 404
    assert (await test_client.put("/device-state/hub-settings")).status == 405
    assert (await test_client.post("/device-control/play", data="{}")).status == 415


async def test_a_non_200_status_reaches_the_client_as_a_response_error(client: Any) -> None:
    gemstone, test_client, _ = client
    await test_client.post("/_sim/mode/normal")
    session = aiohttp.ClientSession()
    try:
        # A route the device does not serve: 404 is the client's hard-error path.
        bad = GemstoneClient("127.0.0.1", session, port=gemstone.port)
        with pytest.raises(GemstoneResponseError):
            await bad._request("GET", "/no-such-route")
    finally:
        await session.close()


# --- The whole stack, over a socket, inside Home Assistant ------------------


async def test_config_flow_and_light_entity_against_the_simulator(
    hass: HomeAssistant,
    sim: Simulator,
    socket_enabled: None,
) -> None:
    """Drive setup end to end the way the browser does.

    Everything else here stops at the client. This runs the real config flow,
    the real coordinator and the real light entity against the simulator over a
    real socket -- the same path `tools/dev_ha.sh` exercises, minus the port 80
    bind that forces the simulator to run under sudo.
    """
    server = TestServer(build_app(sim))
    await server.start_server()
    url = f"http://127.0.0.1:{server.port}"
    try:
        # The port is hardcoded in api.py and the config flow collects only a
        # host, so redirecting base_url is the only way to reach a test server.
        with patch.object(GemstoneClient, "base_url", property(lambda _self: url)):
            result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "127.0.0.1"})
            await hass.async_block_till_done()

            assert result["type"] is FlowResultType.CREATE_ENTRY
            # Titled from hubSettings.bluetoothName, not the bare host.
            assert result["title"] == HUB_SETTINGS["bluetoothName"]

            state = hass.states.get("light.front_roofline")
            assert state is not None
            assert state.state == "on"
            assert state.attributes["brightness"] == 180
            assert state.attributes["rgbw_color"] == (35, 123, 192, 0)
            # The animations are the light's effect list, "off" first.
            assert state.attributes["effect_list"][1:] == EFFECT_LIST
            # The pattern list is this Home Assistant's, empty until a save.
            pattern = hass.states.get("select.front_roofline_pattern")
            assert pattern is not None
            assert pattern.attributes["options"] == []

            await hass.services.async_call(
                "light",
                "turn_on",
                {"entity_id": "light.front_roofline", "rgbw_color": [255, 0, 0, 0]},
                blocking=True,
            )
            await hass.async_block_till_done()

            # The simulator received it, and the entity reflects the reply.
            assert sim.state["colorB"]["value"] == 255
            assert hass.states.get("light.front_roofline").attributes["rgbw_color"] == (255, 0, 0, 0)

            await hass.services.async_call("light", "turn_off", {"entity_id": "light.front_roofline"}, blocking=True)
            await hass.async_block_till_done()

            assert hass.states.get("light.front_roofline").state == "off"
            # Powering off must not discard the scene.
            assert sim.state["colorB"]["value"] == 255
    finally:
        sim.cancel_pending()
        await server.close()


async def test_accepted_reply_mode_reports_ahead_of_the_device(client: Any) -> None:
    """Show what the README's open design question would look like in the UI.

    In `accepted` mode the reply carries the commanded scene while the device
    still reports the old one. `async_apply_result` trusts the reply, so the UI
    jumps forward and the next poll pulls it back -- which is exactly the
    behaviour that would force the coordinator change.
    """
    gemstone, test_client, sim = client
    await test_client.post("/_sim/play-reply-mode?mode=accepted&delay=30")

    reply = await gemstone.async_play_color((255, 0, 0, 0), 200)
    assert reply.rgbw == (255, 0, 0, 0)

    polled = await gemstone.async_get_currently_playing()
    assert polled.rgbw != reply.rgbw
    assert sim.state["colorB"]["value"] == DEFAULT_BLUE


# --- Every documented capability ----------------------------------------------


def test_hub_settings_has_the_real_hub_shape() -> None:
    """The seed carries everything a real Hub2 reports, not just the document's two fields."""
    real_keys = set(REAL_HUB_REPLY["state"]["reported"]["hubSettings"])
    assert real_keys <= set(HUB_SETTINGS)
    assert HUB_SETTINGS["pixelOutputNames"][:2] == ["Front roofline", "Garage"]
    assert parse_hub_settings(reported({"hubSettings": HUB_SETTINGS})).pixel_count == (104, 52, 0, 0)


@pytest.mark.parametrize(("profile", "scene"), [("playlist-playing", "playlist"), ("impulse-playing", "impulse")])
def test_opaque_profiles_parse_as_their_scene(profile: str, scene: str) -> None:
    """The document names these scenes and nothing else; the parser must at least know which is playing."""
    playing = parse_currently_playing(reported({"currentlyPlaying": PROFILES[profile]}))
    assert playing.scene == scene
    assert playing.length_seconds == PROFILES[profile][scene]["lengthInSeconds"]
    assert playing.rgbw is None


@pytest.mark.parametrize("value", [0, 86401, True, "30"])
def test_length_in_seconds_is_bounded(value: Any) -> None:
    reason = validate_command({"playlist": {"lengthInSeconds": value}})
    assert reason is not None
    assert "lengthInSeconds" in reason
    assert validate_command({"playlist": {"lengthInSeconds": 30}}) is None
    assert validate_command({"impulse": "now"}) is not None


def test_clamp_command_brings_ranges_into_bounds_but_not_structure() -> None:
    """The clamp hypothesis fixes values; it must not paper over a malformed command."""
    pattern = {
        **PROFILES["pattern-playing"]["pattern"],
        "brightness": 300,
        "direction": 11,
        "colors": [255] * 21,
        "name": "x" * 40,
    }
    clamped = clamp_command({"pattern": pattern})["pattern"]
    assert clamped["brightness"] == 255
    # Six directions exist; nothing above 5 means anything to an animation.
    assert clamped["direction"] == 5
    assert len(clamped["colors"]) == 20
    assert len(clamped["name"]) == 32
    assert validate_command({"pattern": clamped}) is None
    assert validate_command(clamp_command({"pattern": {**pattern, "colors": "red"}})) is not None
    two_scenes = {"colorB": {"value": 1, "brightness": 1}, "pattern": pattern}
    assert validate_command(clamp_command(two_scenes)) is not None


def test_cli_parsers() -> None:
    assert parse_controller("Garage:8081") == ("Garage", 8081)
    assert parse_controller("Front Roofline:8080") == ("Front Roofline", 8080)
    assert parse_output_names("Front,Garage") == ["Front", "Garage", "", ""]
    for bad in ("nocolon", ":8080", "x:notaport", "x:70000"):
        with pytest.raises(argparse.ArgumentTypeError):
            parse_controller(bad)
    with pytest.raises(argparse.ArgumentTypeError):
        parse_output_names("a,b,c,d,e")


def test_lossy_is_repeatable_with_a_seed() -> None:
    first, second = Simulator("color-on"), Simulator("color-on")
    first.set_lossy(0.5, seed=7)
    second.set_lossy(0.5, seed=7)
    assert [first.lossy_drop() for _ in range(20)] == [second.lossy_drop() for _ in range(20)]
    always = Simulator("color-on")
    always.set_lossy(1.0)
    assert all(always.lossy_drop() for _ in range(5))
    never = Simulator("color-on")
    never.set_lossy(0.0)
    assert not any(never.lossy_drop() for _ in range(5))


async def test_clamp_mode_applies_where_reject_mode_400s(client: Any) -> None:
    """Both readings of Q5 can be rehearsed: the same command is refused, then clamped."""
    gemstone, test_client, _ = client
    command = build_pattern_command("chase", "Chase", [(255, 0, 0, 0)], 255)
    command["pattern"]["speed"] = 999

    with pytest.raises(GemstoneResponseError) as err:
        await gemstone.async_play(command)
    assert err.value.status == 400

    await test_client.post("/_sim/validation?mode=clamp")
    result = await gemstone.async_play(command)
    assert result.pattern is not None
    assert result.pattern["speed"] == 255


async def test_ble_disable_takes_effect_only_after_a_power_cycle(client: Any) -> None:
    """B800 flips the flag; the server keeps serving until a power cycle; B801 brings it back."""
    gemstone, test_client, _ = client
    await test_client.post("/_sim/ble/B800")
    assert (await gemstone.async_get_hub_settings()).tcp_enabled is False
    # Still served: "you must power-cycle the controller for the change to take effect".
    assert (await gemstone.async_get_currently_playing()).on_state is True

    await test_client.post("/_sim/power-cycle?seconds=0.2")
    with pytest.raises(GemstoneConnectionError):
        await gemstone.async_get_currently_playing()
    await asyncio.sleep(0.3)
    # Booted, but with the HTTP server disabled: still unreachable.
    with pytest.raises(GemstoneConnectionError):
        await gemstone.async_get_currently_playing()

    await test_client.post("/_sim/ble/B801")
    assert (await gemstone.async_get_hub_settings()).tcp_enabled is True
    assert (await gemstone.async_get_currently_playing()).on_state is True


async def test_reboot_comes_back_on_new_firmware_with_the_scene_intact(client: Any) -> None:
    gemstone, test_client, _ = client
    await gemstone.async_play_color((255, 0, 0, 0), 200)
    await test_client.post("/_sim/reboot?seconds=0.2&firmware=1.2.0&firmwareSpi=1.1.0")

    with pytest.raises(GemstoneConnectionError):
        await gemstone.async_get_hub_settings()
    await asyncio.sleep(0.3)

    settings = await gemstone.async_get_hub_settings()
    assert settings.firmware == "1.2.0"
    assert settings.firmware_spi == "1.1.0"
    assert settings.firmware_wifi == HUB_SETTINGS["firmwareWifi"]
    assert (await gemstone.async_get_currently_playing()).rgbw == (255, 0, 0, 0)


@pytest.mark.parametrize(("mode", "status"), [("error", 500), ("unauthorized", 401)])
async def test_injected_error_codes_reach_the_client(client: Any, mode: str, status: int) -> None:
    gemstone, test_client, _ = client
    await test_client.post(f"/_sim/mode/{mode}")
    with pytest.raises(GemstoneResponseError) as err:
        await gemstone.async_get_currently_playing()
    assert err.value.status == status


async def test_oversized_body_is_413(client: Any) -> None:
    """413 needs a buffer size the document never gives; the simulator assumes 8 KiB."""
    _, test_client, _ = client
    body = json.dumps({"state": {"desired": {"currentlyPlaying": {"onState": True, "padding": "x" * 9000}}}})
    response = await test_client.post("/device-control/play", data=body, headers={"Content-Type": "application/json"})
    assert response.status == 413


async def test_http_1_0_is_505(client: Any) -> None:
    """Reject an HTTP/1.0 request with 505: only HTTP/1.1 is supported, per the error table."""
    gemstone, _, _ = client
    reader, writer = await asyncio.open_connection("127.0.0.1", gemstone.port)
    try:
        writer.write(b"GET /device-state/hub-settings HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        status_line = await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()
    assert b" 505 " in status_line


async def test_latency_profile_delays_but_succeeds(client: Any) -> None:
    gemstone, test_client, _ = client
    await test_client.post("/_sim/mode/latency?mean=0.25&jitter=0&seed=1")
    started = time.monotonic()
    assert (await gemstone.async_get_currently_playing()).on_state is True
    assert time.monotonic() - started >= 0.2


async def test_flaky_link_alternates_and_ends_normal(client: Any) -> None:
    gemstone, test_client, sim = client
    await test_client.post("/_sim/mode/flaky?down=0.3&up=0.3&cycles=1")
    with pytest.raises(GemstoneConnectionError):
        await gemstone.async_get_currently_playing()

    await asyncio.sleep(0.35)
    assert (await gemstone.async_get_currently_playing()).on_state is True
    assert sim.fault == "flaky"

    await asyncio.sleep(0.35)
    assert (await gemstone.async_get_currently_playing()).on_state is True
    assert sim.fault == "normal"


async def test_busy_burst_of_two_surfaces_as_busy(client: Any) -> None:
    """One 503 is absorbed by the client's retry; two in a row are not."""
    gemstone, test_client, sim = client
    await test_client.post("/_sim/mode/busy-burst?count=1")
    assert (await gemstone.async_get_currently_playing()).on_state is True
    assert sim.fault == "normal"

    await test_client.post("/_sim/mode/busy-burst?count=2")
    with pytest.raises(GemstoneBusyError):
        await gemstone.async_get_currently_playing()
    assert sim.fault == "normal"


async def test_lossy_link_drops_requests(client: Any) -> None:
    gemstone, test_client, _ = client
    await test_client.post("/_sim/mode/lossy?rate=1")
    with pytest.raises(GemstoneConnectionError):
        await gemstone.async_get_currently_playing()
    await test_client.post("/_sim/mode/lossy?rate=0")
    assert (await gemstone.async_get_currently_playing()).on_state is True


async def test_normal_clears_every_profile(client: Any) -> None:
    _, test_client, sim = client
    await test_client.post("/_sim/mode/latency?mean=1&jitter=0")
    await test_client.post("/_sim/mode/flaky?down=1&up=1&cycles=3")
    await test_client.post("/_sim/mode/normal")
    assert sim.fault == "normal"
    assert sim.latency is None
    assert sim.flaky is None


async def test_status_reports_every_mode(client: Any) -> None:
    _, test_client, _ = client
    await test_client.post("/_sim/validation?mode=clamp")
    await test_client.post("/_sim/mode/latency?mean=0.1&jitter=0")
    status = await (await test_client.get("/_sim/status")).json()
    assert status["validation_mode"] == "clamp"
    assert status["latency"] == {"mean": 0.1, "jitter": 0.0}
    assert status["tcp_enabled"] is True
    assert status["http_up"] is True
    assert status["hub_settings"]["pixelOutputNames"][:2] == ["Front roofline", "Garage"]
    assert status["name"] == HUB_SETTINGS["bluetoothName"]


async def test_two_controllers_serve_independently(socket_enabled: None) -> None:
    """One process, one loop, one Simulator per port -- what --controller does."""
    front, garage = Simulator("color-on", name="Front Roofline"), Simulator("off", name="Garage")
    servers = [TestServer(build_app(front)), TestServer(build_app(garage))]
    for server in servers:
        await server.start_server()
    session = aiohttp.ClientSession()
    try:
        first = GemstoneClient("127.0.0.1", session, port=servers[0].port)
        second = GemstoneClient("127.0.0.1", session, port=servers[1].port)
        assert (await first.async_get_hub_settings()).bluetooth_name == "Front Roofline"
        assert (await second.async_get_hub_settings()).bluetooth_name == "Garage"
        await first.async_play_color((255, 0, 0, 0), 100)
        assert (await second.async_get_currently_playing()).on_state is False
        assert garage.state["colorB"]["value"] == DEFAULT_BLUE
    finally:
        await session.close()
        for server in servers:
            await server.close()
        front.cancel_pending()
        garage.cancel_pending()


# --- SDDP ----------------------------------------------------------------------


def test_sddp_message_is_the_firmware_text_byte_for_byte() -> None:
    assert sddp_message(SDDP_OK, "ABC123", "192.168.1.50", tran=1234) == FIRMWARE_RESPONSE
    assert sddp_message(SDDP_ALIVE, "ABC123", "192.168.1.50") == FIRMWARE_ALIVE
    # The one extension, only off port 80, and still something the parser accepts.
    with_port = sddp_message(SDDP_OK, "ABC123", "192.168.1.50", tran=1, http_port=8080)
    assert with_port == FIRMWARE_RESPONSE.replace(b"Tran: 1234", b"Tran: 1") + b"Http-Port: 8080\r\n"
    assert sddp_message(SDDP_OK, "ABC123", "192.168.1.50", tran=1, http_port=80) == with_port.replace(
        b"Http-Port: 8080\r\n", b""
    )
    assert parse_sddp(with_port, ("192.168.1.50", 1902)).http_port == 8080


def test_parse_search_wants_what_the_firmware_wants() -> None:
    assert parse_search(build_search_request("192.168.1.10", 77)) == ("192.168.1.10", 77)
    # The shape an off-the-shelf SDDP client sends; the firmware ignores it, so the simulator must too.
    assert parse_search(b"SEARCH * SDDP/1.0\r\nHost: 192.168.1.10:5000\r\n") is None
    assert parse_search(b'SEARCH * SDDP/1.0\r\nFrom: "192.168.1.10"\r\nTran: 1\r\n') is None
    assert parse_search(b"NOTIFY ALIVE SDDP/1.0\r\n") is None


async def test_sddp_responder_is_silent_while_booting(socket_enabled: None) -> None:
    sim = Simulator("color-on", serial="ABC123")
    transport, responder = await start_sddp([sim], "127.0.0.1", 0)
    port = transport.get_extra_info("sockname")[1]
    try:
        sim.power_cycle(10.0)
        assert await sddp.async_query("127.0.0.1", "127.0.0.1", port=port, wait=0.3) is None
        assert responder.searches == 1
    finally:
        transport.close()


async def test_sddp_responder_ignores_a_search_without_the_firmware_headers(socket_enabled: None) -> None:
    sim = Simulator("color-on", serial="ABC123")
    transport, responder = await start_sddp([sim], "127.0.0.1", 0)
    port = transport.get_extra_info("sockname")[1]
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.bind(("127.0.0.1", 0))
            sender.settimeout(0.3)
            sender.sendto(b"SEARCH * SDDP/1.0\r\nHost: 127.0.0.1:5000\r\n", ("127.0.0.1", port))
            with pytest.raises(TimeoutError):
                sender.recvfrom(2048)
        finally:
            sender.close()
        assert responder.searches == 0
    finally:
        transport.close()
