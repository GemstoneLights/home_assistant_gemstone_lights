"""Diagnostic entity tests: pixel count, firmware versions, and the TCP flag."""

import copy
from datetime import timedelta

import pytest
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gemstone_lights.const import DOMAIN, FAST_SCAN_INTERVAL, HUB_SETTINGS_REFRESH_SECONDS

from .payloads import COLOR_ON_RESPONSE, HUB_SETTINGS_RESPONSE, PLAYING_URL, SETTINGS_URL
from .test_light import ENTITY as LIGHT
from .test_light import setup_integration

SECONDARY = (("sensor", "firmware_spi"), ("sensor", "firmware_wifi"), ("binary_sensor", "tcp_enabled"))


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str) -> str | None:
    """Look the entity up by its unique id, so the test does not guess the slug."""
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")


async def test_pixel_count_sensor_total_and_outputs(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """One diagnostic sensor: the total as state, each output as an attribute."""
    entry = await setup_integration(hass, aioclient_mock)
    entity_id = _entity_id(hass, entry, "sensor", "pixel_count")

    state = hass.states.get(entity_id)
    assert state.state == "156"
    outputs = {key: value for key, value in state.attributes.items() if key.startswith("output_")}
    assert outputs == {"output_1": 104, "output_2": 52, "output_3": 0, "output_4": 0}
    assert er.async_get(hass).async_get(entity_id).entity_category is EntityCategory.DIAGNOSTIC


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_firmware_sensors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = await setup_integration(hass, aioclient_mock)
    assert hass.states.get(_entity_id(hass, entry, "sensor", "firmware_spi")).state == "1.2.1"
    assert hass.states.get(_entity_id(hass, entry, "sensor", "firmware_wifi")).state == "3.3.9"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_tcp_enabled_binary_sensor(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A feature flag, not link state: on, and deliberately without a device class."""
    entry = await setup_integration(hass, aioclient_mock)
    state = hass.states.get(_entity_id(hass, entry, "binary_sensor", "tcp_enabled"))
    assert state.state == STATE_ON
    assert "device_class" not in state.attributes


async def test_secondary_diagnostics_are_disabled_by_default(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Firmware versions and the TCP flag are registered but start disabled; pixel count does not."""
    entry = await setup_integration(hass, aioclient_mock)
    registry = er.async_get(hass)
    for platform, key in SECONDARY:
        entity_id = _entity_id(hass, entry, platform, key)
        assert entity_id is not None
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None
    assert registry.async_get(_entity_id(hass, entry, "sensor", "pixel_count")).disabled_by is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_diagnostic_entities_share_the_light_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await setup_integration(hass, aioclient_mock)
    registry = er.async_get(hass)
    light_device = registry.async_get(LIGHT).device_id
    for platform, key in (("sensor", "pixel_count"), *SECONDARY):
        assert registry.async_get(_entity_id(hass, entry, platform, key)).device_id == light_device


async def test_sensor_absent_when_field_missing(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Older firmware without a field gets no entity for it, not an "unknown" one."""
    sparse = copy.deepcopy(HUB_SETTINGS_RESPONSE)
    hub = sparse["state"]["reported"]["hubSettings"]
    del hub["firmwareSpi"]
    del hub["tcpEnabled"]
    entry = await setup_integration(hass, aioclient_mock, settings=sparse)

    assert _entity_id(hass, entry, "sensor", "firmware_spi") is None
    assert _entity_id(hass, entry, "binary_sensor", "tcp_enabled") is None
    assert _entity_id(hass, entry, "sensor", "firmware_wifi") is not None
    assert _entity_id(hass, entry, "sensor", "pixel_count") is not None


async def test_sensors_follow_hub_settings_refresh(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """When the coordinator re-reads hub settings, the sensors show the new values."""
    entry = await setup_integration(hass, aioclient_mock)
    entity_id = _entity_id(hass, entry, "sensor", "pixel_count")
    grown = copy.deepcopy(HUB_SETTINGS_RESPONSE)
    grown["state"]["reported"]["hubSettings"]["pixelCount"] = [200, 0, 0, 0]
    aioclient_mock.clear_requests()
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)
    aioclient_mock.get(SETTINGS_URL, json=grown)

    entry.runtime_data._hub_settings_read_at -= HUB_SETTINGS_REFRESH_SECONDS + 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=FAST_SCAN_INTERVAL + 1))
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == "200"
    assert state.attributes["output_1"] == 200
    assert state.attributes["output_2"] == 0
