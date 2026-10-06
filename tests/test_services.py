"""Action tests: play_pattern, save_playing and forget_pattern, and the services.yaml contract."""

from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
import yaml
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gemstone_lights.api import rgbw_to_int
from custom_components.gemstone_lights.const import ANIMATIONS, DOMAIN
from custom_components.gemstone_lights.services import (
    FORGET_PATTERN_SCHEMA,
    PLAY_PATTERN_SCHEMA,
    SAVE_PLAYING_SCHEMA,
)

from .payloads import PATTERN_RESPONSE, PLAY_URL
from .test_light import ENTITY, sent_body, setup_integration

SERVICES_YAML = Path(__file__).resolve().parent.parent / "custom_components" / "gemstone_lights" / "services.yaml"

RED = [255, 0, 0, 0]


async def _call(hass: HomeAssistant, service: str, data: dict[str, Any]) -> None:
    await hass.services.async_call(DOMAIN, service, {ATTR_ENTITY_ID: ENTITY, **data}, blocking=True)


# --- play_pattern ---------------------------------------------------------


async def test_play_pattern_action_sends_full_pattern(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Every field the API accepts reaches the wire, in the API's shapes."""
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await _call(
        hass,
        "play_pattern",
        {
            # Smooth is the animation that exercises every field at once: it
            # takes all six directions, has a background, and reads two knobs.
            "animation": "smooth",
            "colors": [RED, [0, 255, 0, 0], [0, 0, 255, 0]],
            "brightness": 200,
            "speed": 220,
            "direction": 2,
            "background_color": [0, 0, 0, 40],
            "name": "Game night",
        },
    )

    playing = sent_body(aioclient_mock)["currentlyPlaying"]
    pattern = playing["pattern"]
    assert pattern["animation"] == "smooth"
    assert pattern["colors"] == [255, 65280, 16711680]
    assert pattern["brightness"] == 200
    assert pattern["speed"] == 220
    assert pattern["direction"] == 2
    assert pattern["backgroundColor"] == rgbw_to_int(0, 0, 0, 40)
    assert pattern["name"] == "Game night"
    assert pattern["extraParameters"] == {
        "version": 0,
        "0": {"value": 10, "name": "Color Length"},
        "1": {"value": 2, "name": "Spacing"},
    }
    assert playing["colorB"] is None
    assert playing["architectural"] is None


async def test_play_pattern_action_accepts_display_name(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The effect picker's names work here too; the slug is what goes out."""
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await _call(hass, "play_pattern", {"animation": "Gradient Wave", "colors": [RED]})

    pattern = sent_body(aioclient_mock)["currentlyPlaying"]["pattern"]
    assert pattern["animation"] == "gradient_wave"
    assert pattern["name"] == "Gradient Wave"


async def test_play_pattern_action_defaults_from_number_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Omitted parameters come from the same place the effect picker reads."""
    entry = await setup_integration(hass, aioclient_mock)
    entry.runtime_data.async_set_effect_params(speed=77)
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await _call(hass, "play_pattern", {"animation": "chase", "colors": [RED]})

    pattern = sent_body(aioclient_mock)["currentlyPlaying"]["pattern"]
    assert pattern["speed"] == 77
    assert pattern["brightness"] == 16  # the colour scene's current brightness
    # Chase's own first direction, and no background: it has none.
    assert pattern["direction"] == 0
    assert "backgroundColor" not in pattern


@pytest.mark.parametrize(
    "data",
    [
        {"animation": "chase", "colors": [RED] * 21},
        {"animation": "chase", "colors": [RED], "direction": 6},
        {"animation": "chase", "colors": [RED], "direction": "Sideways"},
        {"animation": "chase", "colors": [RED], "speed": 0},
        {"animation": "chase", "colors": [RED], "name": "x" * 33},
        {"animation": "chase", "colors": [RED], "name": ""},
        {"animation": "chase", "colors": [[255, 0, 0]]},
        {"animation": "chase", "colors": [[255, 0, 0, 0, 0]]},
        {"animation": "chase", "colors": []},
        {"animation": "chase"},
        {"animation": "disco", "colors": [RED]},
        {"animation": 5, "colors": [RED]},
        {"colors": [RED]},
    ],
)
async def test_play_pattern_action_rejects_bad_input(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, data: dict[str, Any]
) -> None:
    """Schema failures surface as vol.Invalid, before anything is sent."""
    await setup_integration(hass, aioclient_mock)
    calls = len(aioclient_mock.mock_calls)

    with pytest.raises(vol.Invalid):
        await _call(hass, "play_pattern", data)
    assert len(aioclient_mock.mock_calls) == calls


async def test_action_controller_failure_raises_translated_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, status=500)

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "play_pattern", {"animation": "chase", "colors": [RED]})
    assert err.value.translation_key == "command_failed"


# --- services.yaml contract -----------------------------------------------


def test_services_yaml_animation_options_match_catalogue() -> None:
    """The static select options cannot drift from const.ANIMATIONS."""
    spec = yaml.safe_load(SERVICES_YAML.read_text())
    options = spec["play_pattern"]["fields"]["animation"]["selector"]["select"]["options"]
    assert [(option["value"], option["label"]) for option in options] == list(ANIMATIONS.items())


def test_services_yaml_fields_match_schemas() -> None:
    """Every schema field is described, and nothing is described that the schema lacks."""
    spec = yaml.safe_load(SERVICES_YAML.read_text())
    assert set(spec) == {"play_pattern", "save_playing", "forget_pattern"}
    for service, schema in (
        ("play_pattern", PLAY_PATTERN_SCHEMA),
        ("save_playing", SAVE_PLAYING_SCHEMA),
        ("forget_pattern", FORGET_PATTERN_SCHEMA),
    ):
        assert set(spec[service]["fields"]) == {str(key) for key in schema}
        assert spec[service]["target"]["entity"] == {"integration": DOMAIN, "domain": "light"}
