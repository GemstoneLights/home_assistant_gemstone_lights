"""Adaptive interval, reply adoption, effect parameters, hub-settings refresh, options, diagnostics."""

import copy
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gemstone_lights.api import parse_rgbw_int
from custom_components.gemstone_lights.const import (
    DEFAULT_SPEED,
    FAST_SCAN_INTERVAL,
    HUB_SETTINGS_REFRESH_SECONDS,
    MAX_BACKOFF_INTERVAL,
)
from custom_components.gemstone_lights.coordinator import next_interval
from custom_components.gemstone_lights.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .payloads import (
    COLOR_ON_RESPONSE,
    HUB_SETTINGS_RESPONSE,
    OFF_ONLY_RESPONSE,
    PATTERN_RESPONSE,
    PLAY_URL,
    PLAYING_URL,
    SETTINGS_URL,
)
from .test_light import ENTITY, setup_integration


def _settings_with_firmware(version: str) -> dict[str, Any]:
    payload = copy.deepcopy(HUB_SETTINGS_RESPONSE)
    payload["state"]["reported"]["hubSettings"]["firmware"] = version
    return payload


def test_next_interval_fast_after_activity() -> None:
    """Recent activity keeps polling fast."""
    assert next_interval(30, 0, 0.0) == FAST_SCAN_INTERVAL
    assert next_interval(30, 0, 59.9) == FAST_SCAN_INTERVAL


def test_next_interval_idle_when_quiet() -> None:
    """A quiet controller gets the configured idle interval."""
    assert next_interval(30, 0, 61.0) == 30
    assert next_interval(45, 0, 3600.0) == 45


def test_next_interval_backs_off_on_failures_and_caps() -> None:
    """Failures widen the interval toward the cap; recovery resets it."""
    assert next_interval(30, 1, 0.0) == 60
    assert next_interval(30, 2, 0.0) == MAX_BACKOFF_INTERVAL
    assert next_interval(30, 9, 0.0) == MAX_BACKOFF_INTERVAL
    assert next_interval(30, 0, 0.0) == FAST_SCAN_INTERVAL


async def test_setup_starts_in_the_fast_window(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A fresh setup counts as activity, so the first minute polls fast."""
    entry = await setup_integration(hass, aioclient_mock)
    coordinator = entry.runtime_data
    assert coordinator.update_interval is not None
    assert coordinator.update_interval.total_seconds() == FAST_SCAN_INTERVAL


async def test_power_only_reply_carries_the_scene_forward(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A section 3.4 reply has no scene; the previous one stays under the new power state."""
    entry = await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=OFF_ONLY_RESPONSE)

    await hass.services.async_call("light", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: ENTITY}, blocking=True)

    data = entry.runtime_data.data
    assert data.on_state is False
    assert data.scene == "color"
    assert data.rgbw == parse_rgbw_int(9999999)
    assert data.brightness == 16
    # Diagnostics still see the reply's own fields on top of the carried scene.
    assert data.raw["onState"] is False
    assert data.raw["colorB"] == {"value": 9999999, "brightness": 16}


async def test_reply_with_a_scene_replaces_the_previous_one(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Carry-forward is only for scene-less replies; a real scene is adopted as-is."""
    entry = await setup_integration(hass, aioclient_mock)
    aioclient_mock.post(PLAY_URL, json=PATTERN_RESPONSE)

    await hass.services.async_call("light", "turn_on", {ATTR_ENTITY_ID: ENTITY, "effect": "IsoFade"}, blocking=True)

    data = entry.runtime_data.data
    assert data.scene == "pattern"
    assert data.animation == "isofade"


async def test_reported_pattern_updates_effect_params(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The speed the controller reports becomes the store's value."""
    entry = await setup_integration(hass, aioclient_mock, playing=PATTERN_RESPONSE)
    coordinator = entry.runtime_data
    reported = PATTERN_RESPONSE["state"]["reported"]["currentlyPlaying"]["pattern"]["speed"]
    assert coordinator.effect_speed == reported

    slower = copy.deepcopy(PATTERN_RESPONSE)
    slower["state"]["reported"]["currentlyPlaying"]["pattern"]["speed"] = 30
    aioclient_mock.clear_requests()
    aioclient_mock.get(PLAYING_URL, json=slower)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=FAST_SCAN_INTERVAL + 1))
    await hass.async_block_till_done()

    assert coordinator.effect_speed == 30


async def test_odd_reported_params_are_ignored(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A pattern whose speed is not an integer leaves the store alone."""
    odd = copy.deepcopy(PATTERN_RESPONSE)
    odd["state"]["reported"]["currentlyPlaying"]["pattern"]["speed"] = "fast"
    entry = await setup_integration(hass, aioclient_mock, playing=odd)
    assert entry.runtime_data.effect_speed == DEFAULT_SPEED


async def test_colour_scene_leaves_effect_params_alone(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With no pattern reported, the user-set value wins and survives polls."""
    entry = await setup_integration(hass, aioclient_mock)
    coordinator = entry.runtime_data
    assert coordinator.effect_speed == DEFAULT_SPEED

    coordinator.async_set_effect_params(speed=999)
    assert coordinator.effect_speed == 255  # clamped
    coordinator.async_set_effect_params(speed=0)
    assert coordinator.effect_speed == 1  # speed has no zero

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=FAST_SCAN_INTERVAL + 1))
    await hass.async_block_till_done()
    assert coordinator.effect_speed == 1


async def test_hub_settings_refresh_after_recovery(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Coming back from an outage re-reads hub settings: that is when firmware changes."""
    entry = await setup_integration(hass, aioclient_mock)
    coordinator = entry.runtime_data
    assert coordinator.hub_settings.firmware == "1.1.5"

    aioclient_mock.clear_requests()
    aioclient_mock.get(PLAYING_URL, exc=aiohttp.ClientConnectionError("rebooting"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=FAST_SCAN_INTERVAL + 1))
    await hass.async_block_till_done()

    aioclient_mock.clear_requests()
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)
    aioclient_mock.get(SETTINGS_URL, json=_settings_with_firmware("2.0.0"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=MAX_BACKOFF_INTERVAL + 10))
    await hass.async_block_till_done()

    assert coordinator.hub_settings.firmware == "2.0.0"
    assert hass.states.get(ENTITY).state == STATE_ON


async def test_hub_settings_refresh_after_interval(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Without an outage, settings are still re-read once the refresh interval has passed."""
    entry = await setup_integration(hass, aioclient_mock)
    coordinator = entry.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)
    aioclient_mock.get(SETTINGS_URL, json=_settings_with_firmware("2.0.0"))

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=FAST_SCAN_INTERVAL + 1))
    await hass.async_block_till_done()
    assert coordinator.hub_settings.firmware == "1.1.5"  # not yet due

    coordinator._hub_settings_read_at -= HUB_SETTINGS_REFRESH_SECONDS + 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=FAST_SCAN_INTERVAL + 1))
    await hass.async_block_till_done()
    assert coordinator.hub_settings.firmware == "2.0.0"


async def test_hub_settings_refresh_failure_keeps_old_settings(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A failed settings read is not a failed poll: stale settings beat an unavailable light."""
    entry = await setup_integration(hass, aioclient_mock)
    coordinator = entry.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)
    aioclient_mock.get(SETTINGS_URL, exc=aiohttp.ClientConnectionError("busy"))

    coordinator._hub_settings_read_at -= HUB_SETTINGS_REFRESH_SECONDS + 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=FAST_SCAN_INTERVAL + 1))
    await hass.async_block_till_done()

    assert coordinator.hub_settings.firmware == "1.1.5"
    assert hass.states.get(ENTITY).state == STATE_ON


async def test_options_flow_sets_idle_interval(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The options flow stores a clamped idle interval and reloads the entry."""
    entry = await setup_integration(hass, aioclient_mock)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"scan_interval": 45})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"scan_interval": 45}
    assert entry.runtime_data._idle_interval == 45


async def test_diagnostics_redact_private_details(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The home's coordinates and identifiers never leave in a diagnostics file."""
    entry = await setup_integration(hass, aioclient_mock)

    diagnostics: dict[str, Any] = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry_data"]["host"] == "**REDACTED**"
    assert diagnostics["entry_options"] == {}
    assert diagnostics["effect_speed"] == DEFAULT_SPEED
    settings = diagnostics["hub_settings"]
    assert settings["location"] == "**REDACTED**"
    assert settings["localIp"] == "**REDACTED**"
    assert settings["bluetoothName"] == "**REDACTED**"
    assert settings["firmware"] == "1.1.5"
    assert settings["pixelCount"] == [104, 52, 0, 0]
    assert diagnostics["currently_playing"]["onState"] is True
    assert diagnostics["last_update_success"] is True
