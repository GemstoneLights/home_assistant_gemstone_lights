"""Number entity tests: the Animation speed companion."""

import copy

import pytest
from homeassistant.components.number import (
    ATTR_VALUE,
    SERVICE_SET_VALUE,
    NumberExtraStoredData,
)
from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .payloads import PATTERN_RESPONSE, PLAY_URL
from .test_light import ENTITY as LIGHT
from .test_light import sent_body, setup_integration

# Entity ids follow the entity's name, which reads Animation now; the internal
# keys (and so the unique ids) stay effect_*, so nothing existing is orphaned.
SPEED = "number.device_name_animation_speed"


async def _set(hass: HomeAssistant, entity_id: str, value: int) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value}, blocking=True
    )


def _value(hass: HomeAssistant, entity_id: str) -> float:
    return float(hass.states.get(entity_id).state)


def _stored(value: int) -> dict:
    return NumberExtraStoredData(
        native_max_value=255,
        native_min_value=0,
        native_step=1,
        native_unit_of_measurement=None,
        native_value=value,
    ).as_dict()


async def test_number_entities_exist_with_defaults(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Both parameters exist as config entities on the light's device."""
    await setup_integration(hass, aioclient_mock)
    assert _value(hass, SPEED) == 128
    registry = er.async_get(hass)
    entry = registry.async_get(SPEED)
    assert entry.entity_category is EntityCategory.CONFIG
    assert entry.device_id == registry.async_get(LIGHT).device_id
    # Direction is not a control: each animation has its own, and thirteen take
    # none at all.
    assert registry.async_get("number.device_name_animation_direction") is None


async def test_numbers_reflect_reported_pattern(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A pattern the controller reports is real state for the speed number."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    assert _value(hass, SPEED) == 255


async def test_setting_speed_while_pattern_plays_replays_pattern(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Moving the slider during an animation applies live, keeping everything else."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await _set(hass, SPEED, 90)

    pattern = sent_body(aioclient_mock)["currentlyPlaying"]["pattern"]
    assert pattern["speed"] == 90
    assert pattern["animation"] == "isofade"
    assert pattern["colors"] == [255, 65280, 16711680]


async def test_setting_speed_while_colour_plays_only_stores(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With no animation running there is nothing to replay; the value just waits."""
    entry = await setup_integration(hass, aioclient_mock)
    calls_before = len(aioclient_mock.mock_calls)

    await _set(hass, SPEED, 40)

    assert len(aioclient_mock.mock_calls) == calls_before
    assert _value(hass, SPEED) == 40
    assert entry.runtime_data.effect_speed == 40


async def test_setting_speed_while_off_does_not_power_on(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A replay would send onState true; a slider move must never switch the lights on."""
    off_pattern = copy.deepcopy(PATTERN_RESPONSE)
    off_pattern["state"]["reported"]["currentlyPlaying"]["onState"] = False
    await setup_integration(hass, aioclient_mock, playing=off_pattern)
    calls_before = len(aioclient_mock.mock_calls)

    await _set(hass, SPEED, 50)

    assert len(aioclient_mock.mock_calls) == calls_before
    assert hass.states.get(LIGHT).state == STATE_OFF
    assert _value(hass, SPEED) == 50


async def test_values_survive_restart(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """With nothing reported at startup, the remembered value is restored."""
    mock_restore_cache_with_extra_data(hass, [(State(SPEED, "200"), _stored(200))])
    entry = await setup_integration(hass, aioclient_mock)
    assert _value(hass, SPEED) == 200
    assert entry.runtime_data.effect_speed == 200


async def test_reported_value_beats_restored_value(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A pattern reported at startup is real state and outranks the restore cache."""
    mock_restore_cache_with_extra_data(hass, [(State(SPEED, "200"), _stored(200))])
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    assert _value(hass, SPEED) == 255


async def test_set_value_controller_failure_raises(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    aioclient_mock.post(PLAY_URL, status=500)

    with pytest.raises(HomeAssistantError) as err:
        await _set(hass, SPEED, 90)
    assert err.value.translation_key == "command_failed"
