"""Integration setup: discovery flows at start-up, and address-keyed entries learning their serial."""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gemstone_lights.const import CONF_SERIAL, DOMAIN
from custom_components.gemstone_lights.discovery import async_handle_announcement
from custom_components.gemstone_lights.sddp import Kind, SddpDevice

from .payloads import COLOR_ON_RESPONSE, HOST, HUB_SETTINGS_RESPONSE, PLAYING_URL, SETTINGS_URL

NEW_HOST = "192.168.1.99"


def _device(serial: str = "ABC123", host: str = HOST, kind: Kind = "response") -> SddpDevice:
    return SddpDevice(
        serial=serial,
        host=host,
        hostname=f"Gemstone-{serial}",
        kind=kind,
        tran=1,
        max_age=1800,
        http_port=80,
        raw={},
    )


async def test_setup_starts_a_flow_for_each_controller_found(hass: HomeAssistant) -> None:
    """The first search runs at start-up and every controller it finds becomes a Discovered card."""
    with patch("custom_components.gemstone_lights.discovery.async_discover", return_value={"abc123": _device()}):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == config_entries.SOURCE_INTEGRATION_DISCOVERY
    assert flows[0]["context"]["title_placeholders"] == {"name": "Gemstone-ABC123", "host": HOST}
    assert flows[0]["step_id"] == "discovery_confirm"


async def test_setup_entry_learns_the_serial_from_the_controller(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An entry keyed on its address is re-keyed on the serial once the controller answers a query."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    with patch("custom_components.gemstone_lights.discovery.async_query", return_value=_device()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.unique_id == "abc123"
    assert entry.data == {CONF_HOST: HOST, CONF_SERIAL: "ABC123"}
    # Identity did not move: the light is still there under the entry id.
    assert hass.states.get("light.device_name") is not None


async def test_setup_entry_keeps_its_address_key_when_the_controller_is_silent(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.unique_id == HOST
    assert CONF_SERIAL not in entry.data


async def test_setup_entry_does_not_take_a_serial_another_entry_owns(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Two entries for one controller (say, a simulator answering for a real one) must not collide."""
    MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: NEW_HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123"}, unique_id="abc123"
    ).add_to_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    with patch("custom_components.gemstone_lights.discovery.async_query", return_value=_device()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.unique_id == HOST
    assert CONF_SERIAL not in entry.data


async def test_an_announcement_starts_a_flow_but_an_offline_does_not(hass: HomeAssistant) -> None:
    """ALIVE is the heartbeat; IDENTIFY is the app's Control4 Identify button; OFFLINE says nothing useful."""
    assert await async_setup_component(hass, DOMAIN, {})

    async_handle_announcement(hass, _device(kind="offline"))
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    async_handle_announcement(hass, _device(kind="identify"))
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["step_id"] for flow in flows] == ["discovery_confirm"]

    # The heartbeat for the same controller joins the flow already open rather than opening another.
    async_handle_announcement(hass, _device(kind="alive"))
    await hass.async_block_till_done()
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
