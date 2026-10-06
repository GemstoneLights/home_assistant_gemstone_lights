"""Light entity tests: state mapping, each turn_on branch, availability."""

import copy
from datetime import timedelta
from typing import Any

import aiohttp
import pytest
from homeassistant.components.light import EFFECT_OFF, LightEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    CONF_HOST,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gemstone_lights.api import parse_rgbw_int, rgbw_to_int
from custom_components.gemstone_lights.const import DEFAULT_RGBW, DOMAIN, EFFECT_LIST
from custom_components.gemstone_lights.palette import DEFAULT_FAVORITE_COLORS

from .payloads import (
    ARCHITECTURAL_RESPONSE,
    COLOR_OFF_RESPONSE,
    COLOR_ON_RESPONSE,
    HOST,
    HUB_SETTINGS_RESPONSE,
    OFF_ONLY_RESPONSE,
    ON_ONLY_RESPONSE,
    PATTERN_RESPONSE,
    PLAY_URL,
    PLAYING_URL,
    SETTINGS_URL,
)

ENTITY = "light.device_name"


async def setup_integration(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    playing: dict[str, Any] = COLOR_ON_RESPONSE,
    settings: dict[str, Any] = HUB_SETTINGS_RESPONSE,
) -> MockConfigEntry:
    """Set the integration up against a mocked controller."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)
    aioclient_mock.get(SETTINGS_URL, json=settings)
    aioclient_mock.get(PLAYING_URL, json=playing)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_controller_failure_raises_translated_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A command the controller rejects surfaces as a translatable error."""
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, status=500)

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "light",
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: ENTITY, "brightness": 100},
            blocking=True,
        )
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "command_failed"
    assert "500" in err.value.translation_placeholders["error"]


def sent_body(aioclient_mock: AiohttpClientMocker) -> dict[str, Any]:
    """Return the desired currentlyPlaying of the last POST the component sent."""
    method, url, data, _headers = aioclient_mock.mock_calls[-1]
    assert method == "POST"
    # yarl normalises the default :80 away, so compare parts rather than strings.
    assert url.host == HOST
    assert url.path == "/device-control/play"
    return data["state"]["desired"]


async def test_entity_reflects_colour_state(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """ColorB maps to on, brightness, and a full-resolution hue."""
    await setup_integration(hass, aioclient_mock)
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 16
    assert state.attributes["rgbw_color"] == parse_rgbw_int(9999999)


async def test_setup_retries_when_unreachable(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """An unreachable controller puts the entry into setup retry."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST)
    entry.add_to_hass(hass)
    aioclient_mock.get(SETTINGS_URL, exc=aiohttp.ClientConnectionError("no route"))
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_removes_entity(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Unloading the entry makes the entity unavailable."""
    entry = await setup_integration(hass, aioclient_mock)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    state = hass.states.get(ENTITY)
    assert state is None or state.state == STATE_UNAVAILABLE


async def test_bare_turn_on_preserves_scene(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """turn_on with no attributes sends only onState."""
    await setup_integration(hass, aioclient_mock, playing=COLOR_OFF_RESPONSE)
    aioclient_mock.post(PLAY_URL, json=ON_ONLY_RESPONSE)

    await hass.services.async_call("light", SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY}, blocking=True)

    desired = sent_body(aioclient_mock)
    assert desired["origin"] == "homeassistant"
    assert desired["currentlyPlaying"] == {"onState": True}
    assert hass.states.get(ENTITY).state == STATE_ON


async def test_bare_turn_on_keeps_colour_and_brightness_visible(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A power-only reply carries no scene; the card must not go blank until the next poll."""
    await setup_integration(hass, aioclient_mock, playing=COLOR_OFF_RESPONSE)
    aioclient_mock.post(PLAY_URL, json=ON_ONLY_RESPONSE)

    await hass.services.async_call("light", SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY}, blocking=True)

    state = hass.states.get(ENTITY)
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 16
    assert state.attributes["rgbw_color"] == parse_rgbw_int(9999999)


async def test_brightness_only_on_static_colour_replays_colour(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Brightness alone while a colour plays re-sends that colour at the new level."""
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=COLOR_ON_RESPONSE)

    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, "brightness": 200},
        blocking=True,
    )

    playing = sent_body(aioclient_mock)["currentlyPlaying"]
    assert playing["colorB"] == {"value": 9999999, "brightness": 200}
    assert playing["pattern"] is None


async def test_brightness_during_architectural_replays_scene(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Brightness alone while a design plays must not collapse it to one colour."""
    await setup_integration(hass, aioclient_mock, playing=ARCHITECTURAL_RESPONSE)
    aioclient_mock.post(PLAY_URL, json=ARCHITECTURAL_RESPONSE)

    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, "brightness": 100},
        blocking=True,
    )

    playing = sent_body(aioclient_mock)["currentlyPlaying"]
    reported = ARCHITECTURAL_RESPONSE["state"]["reported"]["currentlyPlaying"]["architectural"]
    assert playing["architectural"] == {**reported, "brightness": 100}
    assert playing["colorB"] is None
    assert playing["pattern"] is None


async def test_colour_during_architectural_replaces_scene(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Picking one colour for the whole run is a deliberate replacement of the design."""
    await setup_integration(hass, aioclient_mock, playing=ARCHITECTURAL_RESPONSE)
    aioclient_mock.post(PLAY_URL, json=COLOR_ON_RESPONSE)

    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, "rgbw_color": [255, 0, 0, 0]},
        blocking=True,
    )

    playing = sent_body(aioclient_mock)["currentlyPlaying"]
    assert playing["colorB"]["value"] == 255
    assert playing["architectural"] is None


@pytest.mark.parametrize(
    ("playing", "kind", "played_name"),
    [
        (COLOR_ON_RESPONSE, "color", None),
        (PATTERN_RESPONSE, "pattern", "IsoFade"),
        (ARCHITECTURAL_RESPONSE, "architectural", "Front Roofline Design"),
        (
            {
                "state": {
                    "reported": {
                        "currentlyPlaying": {
                            "playlist": {"name": "Evening", "lengthInSeconds": 30},
                            "onState": True,
                        }
                    }
                }
            },
            "playlist",
            "Evening",
        ),
    ],
)
async def test_playing_attributes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    playing: dict[str, Any],
    kind: str,
    played_name: str | None,
) -> None:
    """``playing`` and ``playing_name`` are the only extra attributes, and track what plays."""
    await setup_integration(hass, aioclient_mock, playing=playing)

    attributes = hass.states.get(ENTITY).attributes
    assert attributes["playing"] == kind
    assert attributes["playing_name"] == played_name
    # The word scene belongs to the saved-pattern entities, not to these.
    assert "scene" not in attributes


async def test_played_name_ignores_a_non_string_name(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    odd = copy.deepcopy(PATTERN_RESPONSE)
    odd["state"]["reported"]["currentlyPlaying"]["pattern"]["name"] = 42
    await setup_integration(hass, aioclient_mock, playing=odd)
    assert hass.states.get(ENTITY).attributes["playing_name"] is None


async def test_handlers_wrap_builder_errors_as_validation_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The service schemas make this unreachable; called directly, the guard still holds."""
    await setup_integration(hass, aioclient_mock)
    entity = hass.data[DATA_INSTANCES]["light"].get_entity(ENTITY)

    with pytest.raises(ServiceValidationError) as err:
        await entity.async_handle_play_pattern(animation="chase", colors=[(255, 0, 0, 0)], name="x" * 33)
    assert err.value.translation_key == "invalid_scene"


async def test_turn_off_sends_on_state_false(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """turn_off sends onState false and the entity follows the reply."""
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=OFF_ONLY_RESPONSE)

    await hass.services.async_call("light", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: ENTITY}, blocking=True)

    assert sent_body(aioclient_mock)["currentlyPlaying"] == {"onState": False}
    assert hass.states.get(ENTITY).state == STATE_OFF


async def test_turn_on_with_colour_sends_color_b(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A colour pick becomes a colorB command that clears the other modes."""
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=COLOR_ON_RESPONSE)

    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, "rgbw_color": [255, 0, 0, 0], "brightness": 128},
        blocking=True,
    )

    playing = sent_body(aioclient_mock)["currentlyPlaying"]
    assert playing["colorB"] == {"value": 255, "brightness": 128}
    assert playing["color"] is None
    assert playing["pattern"] is None
    assert playing["onState"] is True


async def test_colour_only_keeps_last_brightness(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Picking a hue without a brightness holds the current level."""
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=COLOR_ON_RESPONSE)

    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, "rgbw_color": [0, 255, 0, 0]},
        blocking=True,
    )

    playing = sent_body(aioclient_mock)["currentlyPlaying"]
    assert playing["colorB"] == {"value": 65280, "brightness": 16}


async def test_brightness_during_pattern_replays_pattern(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The brightness slider must not kill a running animation."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, "brightness": 99},
        blocking=True,
    )

    playing = sent_body(aioclient_mock)["currentlyPlaying"]
    assert playing["colorB"] is None
    assert playing["pattern"]["animation"] == "isofade"
    assert playing["pattern"]["brightness"] == 99
    assert playing["pattern"]["id"] == "8d8b1e70-4ed4-439e-b096-52446653d758"


async def test_entity_goes_unavailable_and_recovers(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Poll failure marks the light unavailable; recovery brings it back."""
    await setup_integration(hass, aioclient_mock)
    assert hass.states.get(ENTITY).state == STATE_ON

    aioclient_mock.clear_requests()
    aioclient_mock.get(PLAYING_URL, exc=aiohttp.ClientConnectionError("gone"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE

    aioclient_mock.clear_requests()
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)
    # Recovery re-reads hub settings: an outage is what a reboot looks like.
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    # One failure backs the interval off to 60 s, so fire well past that.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=130))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_ON


def favorite_colors(hass: HomeAssistant) -> list[dict[str, Any]] | None:
    """Read the light's favourite colours the way the frontend does."""
    entry = er.async_get(hass).async_get(ENTITY)
    assert entry is not None
    return entry.options.get("light", {}).get("favorite_colors")


async def test_colour_picker_is_seeded_with_the_app_palette(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A new light offers the Gemstone app's swatches, in the app's order."""
    await setup_integration(hass, aioclient_mock)

    assert favorite_colors(hass) == [dict(payload) for _, payload in DEFAULT_FAVORITE_COLORS]


async def test_edited_favourites_are_left_alone(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Colours the user chose survive a reload; seeding happens once, not every start."""
    entry = await setup_integration(hass, aioclient_mock)
    mine = [{"rgbw_color": [1, 2, 3, 4]}]
    er.async_get(hass).async_update_entity_options(ENTITY, "light", {"favorite_colors": mine})

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert favorite_colors(hass) == mine


async def test_cleared_favourites_stay_cleared(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Emptying the row is a choice: the palette is not put back on the next start."""
    entry = await setup_integration(hass, aioclient_mock)
    er.async_get(hass).async_update_entity_options(ENTITY, "light", {"favorite_colors": []})

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert favorite_colors(hass) == []


async def test_a_palette_colour_is_translated_on_the_way_out(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The picker's yellow is the app's yellow; the controller gets the LED-tuned one."""
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=COLOR_ON_RESPONSE)

    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, "rgbw_color": [255, 255, 0, 0], "brightness": 200},
        blocking=True,
    )

    playing = sent_body(aioclient_mock)["currentlyPlaying"]
    assert playing["colorB"] == {"value": rgbw_to_int(255, 87, 0, 0), "brightness": 200}


async def test_a_palette_colour_is_translated_on_the_way_in(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A controller yellow reports as the app's yellow, so its swatch highlights."""
    yellow = copy.deepcopy(COLOR_ON_RESPONSE)
    yellow["state"]["reported"]["currentlyPlaying"]["colorB"] = {
        "value": rgbw_to_int(255, 87, 0, 0),
        "brightness": 255,
    }
    await setup_integration(hass, aioclient_mock, playing=yellow)

    state = hass.states.get(ENTITY)
    assert state is not None
    assert tuple(state.attributes["rgbw_color"]) == (255, 255, 0, 0)


async def test_a_colour_of_your_own_crosses_untouched(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Only palette colours are translated; anything else means what it says."""
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=COLOR_ON_RESPONSE)

    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, "rgbw_color": [18, 52, 86, 120]},
        blocking=True,
    )

    playing = sent_body(aioclient_mock)["currentlyPlaying"]
    assert playing["colorB"]["value"] == rgbw_to_int(18, 52, 86, 120)


async def test_entities_from_shapes_we_dropped_are_removed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Shapes a development build tried must not linger as dead rows.

    Home Assistant keeps a registry entry when its platform goes away, so it
    would sit on the device page unavailable and unexplained. The Animation
    select is the awkward one: select is still a platform here, so a domain rule
    alone would spare it.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    ghosts = [
        registry.async_get_or_create("scene", DOMAIN, f"{entry.entry_id}_pattern_party", config_entry=entry),
        registry.async_get_or_create("select", DOMAIN, f"{entry.entry_id}_animation", config_entry=entry),
    ]
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for ghost in ghosts:
        assert registry.async_get(ghost.entity_id) is None
    # What it does set up is untouched, the Pattern control included.
    surviving = {e.domain for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert surviving == {"light", "number", "select", "sensor", "binary_sensor"}
    selects = {e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "select"}
    assert selects == {f"{entry.entry_id}_saved_pattern"}


# --- effects: the controller's animations, on the light where core expects them


async def test_the_light_advertises_its_animations(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The 29 animations are the effect list, with core's "off" sentinel first.

    Declaring the feature is what gives Google Assistant its Modes trait, lets a
    Home Assistant scene snapshot which animation was playing, and feeds the
    light-effect tile feature (4.7).
    """
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)

    state = hass.states.get(ENTITY)
    assert state.attributes[ATTR_SUPPORTED_FEATURES] & LightEntityFeature.EFFECT
    assert state.attributes["effect_list"] == [EFFECT_OFF, *EFFECT_LIST]
    assert state.attributes["effect"] == "IsoFade"


async def test_a_static_colour_reads_as_no_effect(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Core asks for EFFECT_OFF when nothing is rendered, not None."""
    await setup_integration(hass, aioclient_mock)
    assert hass.states.get(ENTITY).attributes["effect"] == EFFECT_OFF


async def test_an_unknown_animation_slug_reads_as_no_effect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Newer firmware may report an animation we do not know; "off" would be a lie."""
    future = copy.deepcopy(PATTERN_RESPONSE)
    future["state"]["reported"]["currentlyPlaying"]["pattern"]["animation"] = "hologram"
    await setup_integration(hass, aioclient_mock, playing=future)

    assert hass.states.get(ENTITY).attributes["effect"] is None


async def test_picking_an_effect_keeps_the_colours_already_up(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An effect changes only the animation; the palette carries (4.7)."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await hass.services.async_call("light", SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY, "effect": "Chase"}, blocking=True)

    pattern = sent_body(aioclient_mock)["currentlyPlaying"]["pattern"]
    assert pattern["animation"] == "chase"
    assert pattern["name"] == "Chase"
    assert pattern["colors"] == [255, 65280, 16711680]
    assert pattern["brightness"] == 255


async def test_picking_an_effect_with_a_colour_uses_that_colour(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A colour sent with the effect wins over what is playing."""
    await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, "effect": "Fireworks", "rgbw_color": [255, 0, 0, 0], "brightness": 200},
        blocking=True,
    )

    pattern = sent_body(aioclient_mock)["currentlyPlaying"]["pattern"]
    assert pattern["animation"] == "fireworks"
    assert pattern["colors"] == [255]
    assert pattern["brightness"] == 200


async def test_an_effect_uses_the_number_entities(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Speed and direction come from the companion controls."""
    entry = await setup_integration(hass, aioclient_mock)
    entry.runtime_data.async_set_effect_params(speed=200)
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await hass.services.async_call("light", SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY, "effect": "Chase"}, blocking=True)

    pattern = sent_body(aioclient_mock)["currentlyPlaying"]["pattern"]
    assert pattern["speed"] == 200


async def test_an_effect_without_pattern_colours_falls_back_to_the_default(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A reported pattern with no colour list still yields a usable command."""
    colourless = copy.deepcopy(PATTERN_RESPONSE)
    del colourless["state"]["reported"]["currentlyPlaying"]["pattern"]["colors"]
    await setup_integration(hass, aioclient_mock, playing=colourless)
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await hass.services.async_call("light", SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY, "effect": "Chase"}, blocking=True)

    assert sent_body(aioclient_mock)["currentlyPlaying"]["pattern"]["colors"] == [rgbw_to_int(*DEFAULT_RGBW)]


async def test_effect_off_stops_the_animation_with_a_plain_colour(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The API has no stop, and the power toggle would keep the pattern running."""
    await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    aioclient_mock.post(PLAY_URL, json=COLOR_ON_RESPONSE)

    await hass.services.async_call(
        "light", SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY, "effect": EFFECT_OFF}, blocking=True
    )

    playing = sent_body(aioclient_mock)["currentlyPlaying"]
    assert playing["pattern"] is None
    assert playing["colorB"] is not None


async def test_an_unknown_effect_is_a_validation_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Bad user input is a ServiceValidationError, and nothing is sent."""
    await setup_integration(hass, aioclient_mock)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "light", SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY, "effect": "Disco"}, blocking=True
        )
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "unknown_effect"
    assert err.value.translation_placeholders == {"effect": "Disco"}
    assert not any(method == "POST" for method, *_ in aioclient_mock.mock_calls)
