"""Saved patterns: kept by Home Assistant, replayed from the Pattern control."""

import copy
from typing import Any

import pytest
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gemstone_lights.const import DOMAIN, EFFECT_LIST

from .payloads import COLOR_ON_RESPONSE, PATTERN_RESPONSE, PLAY_URL
from .test_light import ENTITY, sent_body, setup_integration

PATTERN = "select.device_name_pattern"
PLAYING_PATTERN = PATTERN_RESPONSE["state"]["reported"]["currentlyPlaying"]["pattern"]


async def save(hass: HomeAssistant, **data: Any) -> dict[str, Any] | None:
    """Run the save action and return its response."""
    response = await hass.services.async_call(
        DOMAIN, "save_playing", {ATTR_ENTITY_ID: ENTITY, **data}, blocking=True, return_response=True
    )
    await hass.async_block_till_done()
    return None if response is None else response[ENTITY]


async def forget(hass: HomeAssistant, name: str) -> None:
    await hass.services.async_call(DOMAIN, "forget_pattern", {ATTR_ENTITY_ID: ENTITY, "name": name}, blocking=True)
    await hass.async_block_till_done()


async def pick(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: PATTERN, ATTR_OPTION: option}, blocking=True
    )
    await hass.async_block_till_done()


async def play_effect(hass: HomeAssistant, effect: str) -> None:
    """Pick an animation the way Home Assistant's own control does."""
    await hass.services.async_call("light", SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY, "effect": effect}, blocking=True)
    await hass.async_block_till_done()


def options(hass: HomeAssistant) -> list[str]:
    """Return what the Pattern control offers."""
    state = hass.states.get(PATTERN)
    assert state is not None
    return state.attributes["options"]


def value(hass: HomeAssistant) -> str:
    """Return the Pattern control's current value."""
    state = hass.states.get(PATTERN)
    assert state is not None
    return state.state


async def test_the_control_holds_only_what_was_saved(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The animations are the light's effect list; this control is separate."""
    await setup_integration(hass, aioclient_mock)

    assert options(hass) == []
    assert hass.states.get(ENTITY).attributes["effect_list"][1:] == EFFECT_LIST


async def test_a_pattern_may_share_an_animation_s_name(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Two lists means no collision: "Chase" can be both, without shadowing."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)

    await save(hass, name="Chase")

    assert options(hass) == ["Chase"]
    assert hass.states.get(ENTITY).attributes["effect_list"].count("Chase") == 1


async def test_saving_adds_it_to_the_control(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """One action while a pattern plays, and it is there to replay."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    assert options(hass) == []

    result = await save(hass, name="Party")

    assert result == {"name": "Party", "slug": "party"}
    assert options(hass) == ["Party"]
    assert value(hass) == "Party"


async def test_a_saved_pattern_replays_exactly(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Picking it sends the pattern the controller reported, untouched."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    await save(hass, name="Party")
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await pick(hass, "Party")

    sent = sent_body(aioclient_mock)["currentlyPlaying"]["pattern"]
    assert sent["animation"] == PLAYING_PATTERN["animation"]
    assert sent["colors"] == PLAYING_PATTERN["colors"]
    assert sent["speed"] == PLAYING_PATTERN["speed"]
    # Including the app's own bookkeeping, which a rebuilt command would lose.
    assert sent["referencePatternId"] == PLAYING_PATTERN["referencePatternId"]


async def test_the_control_still_names_the_palette_after_an_effect_change(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A pattern and an animation combine, and the two controls say so together.

    Once the effect changes the controller reports the new animation's name, so
    the pattern is no longer identifiable by name. Its colours are still on the
    lights, and that is what this control matches on -- so the light reads
    "Fireworks" while Pattern still reads "Canada Day", which is what is
    happening.
    """
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    await save(hass, name="Canada Day")

    combined = copy.deepcopy(PATTERN_RESPONSE)
    playing = combined["state"]["reported"]["currentlyPlaying"]["pattern"]
    playing["animation"] = "fireworks"
    playing["name"] = "Fireworks"
    aioclient_mock.post(PLAY_URL, json=combined)

    await play_effect(hass, "Fireworks")

    assert sent_body(aioclient_mock)["currentlyPlaying"]["pattern"]["animation"] == "fireworks"
    assert hass.states.get(ENTITY).attributes["effect"] == "Fireworks"
    assert value(hass) == "Canada Day"


async def test_a_pattern_whose_colours_are_gone_leaves_the_control_empty(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Matching on colours must not claim a pattern that is not playing."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    await save(hass, name="Canada Day")

    other = copy.deepcopy(PATTERN_RESPONSE)
    playing = other["state"]["reported"]["currentlyPlaying"]["pattern"]
    playing["colors"] = [16711680]
    playing["name"] = "Something else"
    aioclient_mock.post(PLAY_URL, json=other)

    await play_effect(hass, "Chase")

    assert value(hass) == "unknown"


async def test_a_plain_colour_leaves_the_control_empty(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A static colour is not a pattern."""
    await setup_integration(hass, aioclient_mock, playing=COLOR_ON_RESPONSE)
    assert value(hass) == "unknown"


async def test_saved_patterns_survive_a_restart(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """They are kept in Home Assistant's own storage, not in memory."""
    entry = await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    await save(hass, name="Party")

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert options(hass) == ["Party"]


async def test_saving_the_same_name_replaces_it(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Two entries with one name could not be told apart, so the second wins."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)

    await save(hass, name="Party")
    await save(hass, name="Party")

    assert options(hass) == ["Party"]


async def test_saving_needs_a_pattern(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A plain colour is not a pattern, and there is nothing to keep."""
    await setup_integration(hass, aioclient_mock, playing=COLOR_ON_RESPONSE)

    with pytest.raises(ServiceValidationError):
        await save(hass)
    assert options(hass) == []


async def test_forgetting_takes_it_out(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Forget it by name and it leaves the control and the storage."""
    entry = await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    await save(hass, name="Party")

    await forget(hass, "Party")

    assert options(hass) == []
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert options(hass) == []


async def test_forgetting_something_that_is_not_there_says_so(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Silence would hide a typo."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)

    with pytest.raises(ServiceValidationError) as err:
        await forget(hass, "Never saved")
    assert err.value.translation_key == "unknown_pattern"
