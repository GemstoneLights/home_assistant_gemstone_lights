"""Unit tests for the Gemstone Hub2 local HTTP client.

Fixtures are the payloads the controller returns, so a firmware change that
breaks the contract breaks these tests. Nothing here touches the network.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import aiohttp
import pytest

from custom_components.gemstone_lights import api
from custom_components.gemstone_lights.api import (
    GemstoneBusyError,
    GemstoneClient,
    GemstoneConnectionError,
    GemstoneParseError,
    GemstoneResponseError,
    build_architectural_replay_command,
    build_color_command,
    build_on_state_command,
    build_pattern_command,
    build_pattern_replay_command,
    build_play_payload,
    full_scale_rgbw,
    get_brightness_from_rgbw_int,
    parse_currently_playing,
    parse_hub_settings,
    parse_rgbw_int,
    rgbw_to_int,
)

# --- Fixtures lifted from the API document -------------------------------


HUB_SETTINGS_RESPONSE: dict[str, Any] = {
    "state": {
        "reported": {
            "hubSettings": {
                "pixelCount": [104, 0, 0, 0],
                "reversePixels": [False, False, False, False],
                "pixelOutputNames": ["", "", "", ""],
                "localIp": "192.168.1.50",
                "rgbwSequence": "RGBW",
                "timeZone": "-07:00",
                "dstActive": "true",
                "dstMode": "auto",
                "bluetoothName": "Device Name",
                "tcpEnabled": True,
                "location": {"name": "Calgary, AB", "lat": 51.05, "long": -114.07},
                "firmware": "1.1.5",
                "firmwareSpi": "1.2.1",
                "firmwareWifi": "3.3.9",
                "network": {"interface": "wifi-example", "preferred": "auto"},
            },
            "origin": "http",
            "env": "prod",
        }
    }
}

COLOR_B_RESPONSE: dict[str, Any] = {
    "state": {
        "reported": {
            "currentlyPlaying": {
                "color": None,
                "pattern": None,
                "architectural": None,
                "onState": True,
                "colorB": {"value": 9999999, "brightness": 16},
            },
            "env": "prod",
        }
    }
}

PATTERN_RESPONSE: dict[str, Any] = {
    "state": {
        "desired": {"origin": "mqtt"},
        "reported": {
            "currentlyPlaying": {
                "color": None,
                "colorB": None,
                "architectural": None,
                "playlist": None,
                "onState": True,
                "pattern": {
                    "name": "IsoFade",
                    "animation": "isofade",
                    "id": "8d8b1e70-4ed4-439e-b096-52446653d758",
                    "referencePatternId": "93a145b7-e526-4a3c-b315-b1dbb6193eab",
                    "backgroundColor": 0,
                    "brightness": 255,
                    "speed": 255,
                    "colors": [255, 65280, 16711680],
                    "extraParameters": {"0": {"value": 2}, "1": {"value": 5}, "version": 0},
                },
            },
            "origin": "mqtt",
            "env": "prod",
        },
    }
}

ON_STATE_RESPONSE: dict[str, Any] = {"state": {"reported": {"currentlyPlaying": {"onState": True}, "env": "prod"}}}

# Section 3.3 of the Control4 document, verbatim.
ARCHITECTURAL_RESPONSE: dict[str, Any] = {
    "state": {
        "reported": {
            "currentlyPlaying": {
                "onState": True,
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
            },
            "env": "prod",
        }
    }
}


# --- Colour encoding -----------------------------------------------------


def test_documented_single_channel_values() -> None:
    """The document's reference table must round-trip exactly."""
    assert rgbw_to_int(255, 0, 0, 0) == 255
    assert rgbw_to_int(0, 255, 0, 0) == 65280
    assert rgbw_to_int(0, 0, 255, 0) == 16711680
    assert rgbw_to_int(0, 0, 0, 255) == 4278190080


def test_pure_red_is_not_the_hex_value() -> None:
    """Guard the hex-versus-decimal trap the document calls out.

    ``#FF0000`` is 16711680 in hex terms, but in this RGBW layout that integer
    is pure blue. Getting this backwards silently swaps two channels.
    """
    assert rgbw_to_int(255, 0, 0, 0) == 255
    assert parse_rgbw_int(16711680) == (0, 0, 255, 0)


@pytest.mark.parametrize("rgbw", [(0, 0, 0, 0), (255, 255, 255, 255), (18, 52, 86, 120), (1, 2, 3, 4)])
def test_roundtrip(rgbw: tuple[int, int, int, int]) -> None:
    assert parse_rgbw_int(rgbw_to_int(*rgbw)) == rgbw


def test_brightness_is_the_largest_channel() -> None:
    assert get_brightness_from_rgbw_int(rgbw_to_int(10, 200, 30, 0)) == 200
    assert get_brightness_from_rgbw_int(0) == 0


def test_full_scale_rgbw_lifts_a_dimmed_colour() -> None:
    """A colour dimmed to 50% should report full-scale hue plus brightness."""
    dimmed = rgbw_to_int(128, 64, 0, 0)
    assert full_scale_rgbw(dimmed) == (255, 128, 0, 0)


@pytest.mark.parametrize("value", [0, rgbw_to_int(255, 128, 0, 0)])
def test_full_scale_rgbw_leaves_edges_alone(value: int) -> None:
    """Black and already-full-scale colours must pass through untouched."""
    assert full_scale_rgbw(value) == parse_rgbw_int(value)


# --- Parsing -------------------------------------------------------------


def test_parse_hub_settings() -> None:
    settings = parse_hub_settings(HUB_SETTINGS_RESPONSE)
    assert settings.pixel_count == (104, 0, 0, 0)
    assert settings.firmware == "1.1.5"
    assert settings.local_ip == "192.168.1.50"
    assert settings.tcp_enabled is True
    assert settings.bluetooth_name == "Device Name"


def test_parse_color_b_keeps_hue_and_brightness_apart() -> None:
    """``colorB`` already separates the two, so neither needs rescaling."""
    playing = parse_currently_playing(COLOR_B_RESPONSE)
    assert playing.on_state is True
    assert playing.rgbw == parse_rgbw_int(9999999)
    assert playing.brightness == 16
    assert playing.animation is None


def test_parse_legacy_color_splits_baked_brightness() -> None:
    """An old ``color`` integer must be unpacked into hue plus brightness."""
    payload = {"state": {"reported": {"currentlyPlaying": {"color": rgbw_to_int(128, 64, 0, 0), "onState": True}}}}
    playing = parse_currently_playing(payload)
    assert playing.rgbw == (255, 128, 0, 0)
    assert playing.brightness == 128


def test_parse_pattern_exposes_effect_and_first_colour() -> None:
    playing = parse_currently_playing(PATTERN_RESPONSE)
    assert playing.animation == "isofade"
    assert playing.brightness == 255
    # First colour of the pattern, so the frontend card is not blank.
    assert playing.rgbw == (255, 0, 0, 0)
    assert playing.pattern is not None
    assert playing.pattern["name"] == "IsoFade"


def test_parse_on_state_only_response() -> None:
    """A turn-on response carries nothing but ``onState``."""
    playing = parse_currently_playing(ON_STATE_RESPONSE)
    assert playing.on_state is True
    assert playing.rgbw is None
    assert playing.brightness is None
    assert playing.animation is None


def test_parse_off_state() -> None:
    payload = {"state": {"reported": {"currentlyPlaying": {"onState": False}}}}
    assert parse_currently_playing(payload).on_state is False


def test_parse_architectural_exposes_first_colour_and_brightness() -> None:
    """An architectural scene surfaces its first segment's colour, unscaled."""
    playing = parse_currently_playing(ARCHITECTURAL_RESPONSE)
    assert playing.scene == "architectural"
    assert playing.brightness == 255
    # Brightness is a separate field, so the colour is the hue as sent.
    assert playing.rgbw == parse_rgbw_int(12614435)
    assert playing.animation is None
    assert playing.pattern is None
    assert playing.architectural is not None
    assert playing.architectural["name"] == "Front Roofline Design"


@pytest.mark.parametrize(
    ("payload", "scene"),
    [
        (COLOR_B_RESPONSE, "color"),
        ({"state": {"reported": {"currentlyPlaying": {"color": 255, "onState": True}}}}, "color"),
        (PATTERN_RESPONSE, "pattern"),
        (ARCHITECTURAL_RESPONSE, "architectural"),
        (
            {"state": {"reported": {"currentlyPlaying": {"playlist": {"lengthInSeconds": 30}, "onState": True}}}},
            "playlist",
        ),
        (
            {"state": {"reported": {"currentlyPlaying": {"impulse": {"lengthInSeconds": 5}, "onState": True}}}},
            "impulse",
        ),
        (ON_STATE_RESPONSE, None),
    ],
)
def test_parse_sets_scene_discriminator(payload: dict[str, Any], scene: str | None) -> None:
    """``scene`` names the shape that was reported; a power-only reply has none."""
    assert parse_currently_playing(payload).scene == scene


@pytest.mark.parametrize(
    "payload",
    [{}, {"state": {}}, {"state": {"desired": {}}}, [], "nope"],
)
def test_malformed_responses_raise(payload: Any) -> None:
    with pytest.raises(GemstoneParseError):
        parse_currently_playing(payload)


def test_hub_settings_without_the_object_raises() -> None:
    with pytest.raises(GemstoneParseError):
        parse_hub_settings({"state": {"reported": {"hubSettings": "nope"}}})


def test_hub_settings_tolerates_an_odd_pixel_count() -> None:
    """A pixelCount that is not a list parses as no outputs rather than failing setup."""
    settings = parse_hub_settings({"state": {"reported": {"hubSettings": {"pixelCount": 104}}}})
    assert settings.pixel_count == ()


def test_currently_playing_null_or_absent_is_off() -> None:
    assert parse_currently_playing({"state": {"reported": {"currentlyPlaying": None}}}).on_state is False
    assert parse_currently_playing({"state": {"reported": {}}}).scene is None


def test_playlist_and_impulse_are_opaque_scenes() -> None:
    """The document names them and bounds lengthInSeconds; nothing else can be derived."""
    playing = parse_currently_playing(
        {
            "state": {
                "reported": {
                    "currentlyPlaying": {"playlist": {"name": "Evening", "lengthInSeconds": 30}, "onState": True}
                }
            }
        }
    )
    assert playing.scene == "playlist"
    assert playing.length_seconds == 30
    assert playing.rgbw is None
    assert playing.brightness is None
    assert playing.animation is None

    odd = parse_currently_playing(
        {"state": {"reported": {"currentlyPlaying": {"impulse": {"lengthInSeconds": "5"}, "onState": True}}}}
    )
    assert odd.scene == "impulse"
    assert odd.length_seconds is None


def test_currently_playing_that_is_not_an_object_raises() -> None:
    with pytest.raises(GemstoneParseError):
        parse_currently_playing({"state": {"reported": {"currentlyPlaying": "on"}}})


@pytest.mark.parametrize(
    ("pattern", "brightness", "rgbw"),
    [
        ({"animation": "chase", "brightness": "bright", "colors": [255]}, None, (255, 0, 0, 0)),
        ({"animation": "chase", "brightness": 100, "colors": "red"}, 100, None),
        ({"animation": "chase", "brightness": 100, "colors": []}, 100, None),
        ({"animation": "chase", "brightness": 100, "colors": ["red"]}, 100, None),
        ({"animation": "chase"}, None, None),
    ],
    ids=["text-brightness", "text-colours", "no-colours", "text-colour", "bare"],
)
def test_pattern_parsing_tolerates_unexpected_shapes(
    pattern: dict[str, Any], brightness: int | None, rgbw: tuple[int, int, int, int] | None
) -> None:
    """The document says shapes may change; odd fields degrade to None, not to an error."""
    playing = parse_currently_playing(
        {"state": {"reported": {"currentlyPlaying": {"pattern": pattern, "onState": True}}}}
    )
    assert playing.scene == "pattern"
    assert playing.animation == "chase"
    assert playing.brightness == brightness
    assert playing.rgbw == rgbw


@pytest.mark.parametrize(
    "architectural",
    [
        {"name": "x", "brightness": "dim", "staticColors": "none"},
        {"name": "x", "brightness": 50, "staticColors": []},
        {"name": "x", "brightness": 50, "staticColors": ["red"]},
        {"name": "x", "brightness": 50, "staticColors": [{"lights": [1], "color": "red"}]},
    ],
    ids=["text-fields", "no-segments", "text-segment", "text-colour"],
)
def test_architectural_parsing_tolerates_unexpected_shapes(architectural: dict[str, Any]) -> None:
    payload = {"state": {"reported": {"currentlyPlaying": {"architectural": architectural, "onState": True}}}}
    playing = parse_currently_playing(payload)
    assert playing.scene == "architectural"
    assert playing.rgbw is None


# --- Command construction ------------------------------------------------


def test_play_payload_wraps_in_desired_state_with_origin() -> None:
    payload = build_play_payload({"onState": True}, origin="homeassistant")
    assert payload == {"state": {"desired": {"origin": "homeassistant", "currentlyPlaying": {"onState": True}}}}


def test_color_command_clears_the_other_actions() -> None:
    """Exactly one of colorB/pattern/architectural may be set; rest are null."""
    command = build_color_command((255, 0, 0, 0), 128)
    assert command["colorB"] == {"value": 255, "brightness": 128}
    assert command["color"] is None
    assert command["pattern"] is None
    assert command["architectural"] is None
    assert command["onState"] is True


def test_color_command_clamps_brightness() -> None:
    assert build_color_command((1, 2, 3, 4), 999)["colorB"]["brightness"] == 255
    assert build_color_command((1, 2, 3, 4), -5)["colorB"]["brightness"] == 0


def test_pattern_command_shape() -> None:
    command = build_pattern_command("isofade", "IsoFade", [(255, 0, 0, 0), (0, 255, 0, 0)], 200)
    pattern = command["pattern"]
    assert pattern["animation"] == "isofade"
    assert pattern["name"] == "IsoFade"
    assert pattern["colors"] == [255, 65280]
    assert pattern["brightness"] == 200
    assert command["colorB"] is None
    assert command["onState"] is True


def test_pattern_ids_are_distinct_uuid4() -> None:
    """The document requires valid UUID v4 for both id fields."""
    pattern = build_pattern_command("chase", "Chase", [(255, 0, 0, 0)], 255)["pattern"]
    assert UUID(pattern["id"]).version == 4
    assert UUID(pattern["referencePatternId"]).version == 4
    assert pattern["id"] != pattern["referencePatternId"]


def test_pattern_command_caps_colours_at_twenty() -> None:
    command = build_pattern_command("chase", "Chase", [(255, 0, 0, 0)] * 25, 255)
    assert len(command["pattern"]["colors"]) == 20


def test_pattern_command_requires_a_colour() -> None:
    with pytest.raises(ValueError):
        build_pattern_command("chase", "Chase", [], 255)


def test_pattern_replay_overrides_only_brightness() -> None:
    """Replaying a reported pattern keeps everything but the brightness."""
    reported = {
        "name": "IsoFade",
        "animation": "isofade",
        "id": "8d8b1e70-4ed4-439e-b096-52446653d758",
        "colors": [255, 65280],
        "brightness": 255,
        "speed": 200,
        "extraParameters": {"0": {"value": 2}, "version": 0},
    }
    command = build_pattern_replay_command(reported, 300)
    assert command["pattern"]["brightness"] == 255  # clamped from 300
    command = build_pattern_replay_command(reported, 40)
    assert command["pattern"] == {**reported, "brightness": 40}
    assert command["colorB"] is None
    assert command["color"] is None
    assert command["onState"] is True
    # The reported dict itself must not be mutated.
    assert reported["brightness"] == 255


def test_pattern_replay_without_brightness_is_verbatim() -> None:
    reported = {"animation": "chase", "brightness": 128, "colors": [255]}
    assert build_pattern_replay_command(reported)["pattern"] == reported


def test_on_state_command_only_toggles_power() -> None:
    """Power must not disturb the running scene, so nothing else is sent."""
    assert build_on_state_command(False) == {"onState": False}


def test_pattern_command_rejects_bad_name() -> None:
    """The document bounds ``name`` at 1-32 characters."""
    with pytest.raises(ValueError):
        build_pattern_command("chase", "", [(255, 0, 0, 0)], 255)
    with pytest.raises(ValueError):
        build_pattern_command("chase", "x" * 33, [(255, 0, 0, 0)], 255)
    assert build_pattern_command("chase", "x" * 32, [(255, 0, 0, 0)], 255)["pattern"]["name"] == "x" * 32


@pytest.mark.parametrize("direction", [3, 5])
def test_pattern_command_takes_a_direction_the_animation_allows(direction: int) -> None:
    """Chase accepts all six, so the caller's choice is sent as asked."""
    command = build_pattern_command("chase", "Chase", [(255, 0, 0, 0)], 255, direction=direction)
    assert command["pattern"]["direction"] == direction


@pytest.mark.parametrize("direction", [2, 5, 99, None])
def test_a_direction_the_animation_rejects_falls_back_to_its_own(direction: int | None) -> None:
    """Pulse only goes Right or Left; anything else becomes its first.

    A fallback rather than an error: the caller is choosing for whatever plays
    next, and every animation has a sensible default.
    """
    command = build_pattern_command("pulse", "Pulse", [(255, 0, 0, 0)], 255, direction=direction)
    assert command["pattern"]["direction"] == 0


def test_an_unknown_animation_is_rejected() -> None:
    """The catalog decides the shape, so an id it does not know cannot be built."""
    with pytest.raises(ValueError):
        build_pattern_command("hologram", "Hologram", [(255, 0, 0, 0)], 255)


def test_pattern_replay_overrides_speed() -> None:
    """The Animation speed control replays a running pattern with one field changed."""
    reported = {"animation": "chase", "brightness": 128, "speed": 100, "direction": 0, "colors": [255]}
    assert build_pattern_replay_command(reported, speed=300)["pattern"]["speed"] == 255  # clamped
    assert build_pattern_replay_command(reported, speed=0)["pattern"]["speed"] == 1  # speed has no zero
    assert reported["speed"] == 100


def test_architectural_replay_overrides_only_brightness() -> None:
    reported = ARCHITECTURAL_RESPONSE["state"]["reported"]["currentlyPlaying"]["architectural"]
    command = build_architectural_replay_command(reported, 300)
    assert command["architectural"]["brightness"] == 255  # clamped from 300
    command = build_architectural_replay_command(reported, 40)
    assert command["architectural"] == {**reported, "brightness": 40}
    assert command["colorB"] is None
    assert command["pattern"] is None
    assert command["onState"] is True
    # The reported dict itself must not be mutated.
    assert reported["brightness"] == 255


def test_architectural_replay_without_brightness_is_verbatim() -> None:
    reported = {"name": "Design", "brightness": 128, "staticColors": [{"lights": [1], "color": 255}]}
    assert build_architectural_replay_command(reported)["architectural"] == reported


# --- Client --------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: str | None = None) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeRaiser:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Stands in for aiohttp.ClientSession, recording what was sent."""

    def __init__(self, *outcomes: Any) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, *, json: Any = None, timeout: Any = None) -> Any:
        self.calls.append({"method": method, "url": url, "json": json})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            return _FakeRaiser(outcome)
        return outcome


def _client(session: _FakeSession) -> GemstoneClient:
    return GemstoneClient("192.168.1.50", cast(aiohttp.ClientSession, session))


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the busy-retry backoff from slowing the suite down."""
    monkeypatch.setattr(api, "BUSY_RETRY_DELAY", 0)


async def test_get_currently_playing_hits_the_documented_route() -> None:
    session = _FakeSession(_FakeResponse(200, COLOR_B_RESPONSE))
    playing = await _client(session).async_get_currently_playing()

    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"] == "http://192.168.1.50:80/device-state/currently-playing"
    assert playing.brightness == 16


async def test_play_color_posts_a_desired_shadow_document() -> None:
    session = _FakeSession(_FakeResponse(200, COLOR_B_RESPONSE))
    await _client(session).async_play_color((255, 0, 0, 0), 128)

    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://192.168.1.50:80/device-control/play"
    desired = call["json"]["state"]["desired"]
    assert desired["origin"] == "homeassistant"
    assert desired["currentlyPlaying"]["colorB"] == {"value": 255, "brightness": 128}


async def test_play_response_is_parsed_as_new_state() -> None:
    """The POST reply carries reported state, so no extra read is needed."""
    session = _FakeSession(_FakeResponse(200, PATTERN_RESPONSE))
    playing = await _client(session).async_play_pattern("isofade", "IsoFade", [(255, 0, 0, 0)], 255)
    assert playing.animation == "isofade"
    assert len(session.calls) == 1


async def test_busy_controller_is_retried_once() -> None:
    session = _FakeSession(_FakeResponse(503, None), _FakeResponse(200, ON_STATE_RESPONSE))
    playing = await _client(session).async_set_on_state(True)

    assert playing.on_state is True
    assert len(session.calls) == 2


async def test_persistently_busy_controller_raises() -> None:
    session = _FakeSession(_FakeResponse(503, None), _FakeResponse(503, None))
    with pytest.raises(GemstoneBusyError):
        await _client(session).async_get_currently_playing()
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 404, 405, 411, 413, 415, 500, 505])
async def test_documented_error_codes_surface_with_their_status(status: int) -> None:
    session = _FakeSession(_FakeResponse(status, None))
    with pytest.raises(GemstoneResponseError) as err:
        await _client(session).async_get_hub_settings()
    assert err.value.status == status


async def test_unreachable_controller_raises_connection_error() -> None:
    session = _FakeSession(aiohttp.ClientConnectionError("no route to host"))
    with pytest.raises(GemstoneConnectionError):
        await _client(session).async_get_hub_settings()


async def test_timeout_raises_connection_error() -> None:
    session = _FakeSession(TimeoutError())
    with pytest.raises(GemstoneConnectionError):
        await _client(session).async_get_hub_settings()


async def test_invalid_json_raises_parse_error() -> None:
    session = _FakeSession(_FakeResponse(200, ValueError("not json")))
    with pytest.raises(GemstoneParseError):
        await _client(session).async_get_hub_settings()


async def test_a_json_array_body_raises_parse_error() -> None:
    """Valid JSON that is not an object is still not a shadow document."""
    session = _FakeSession(_FakeResponse(200, [1, 2, 3]))
    with pytest.raises(GemstoneParseError):
        await _client(session).async_get_hub_settings()
