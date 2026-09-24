"""Config flow tests: setup by IP, failure mapping, duplicates, reconfigure."""

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import aiohttp
from homeassistant import config_entries
from homeassistant.const import CONF_DEVICE, CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gemstone_lights.config_flow import MANUAL
from custom_components.gemstone_lights.const import CONF_SERIAL, DOMAIN
from custom_components.gemstone_lights.sddp import SddpDevice

from .payloads import COLOR_ON_RESPONSE, HOST, HUB_SETTINGS_RESPONSE, PLAYING_URL, SETTINGS_URL

NEW_HOST = "192.168.1.99"
NEW_SETTINGS_URL = f"http://{NEW_HOST}:80/device-state/hub-settings"
NEW_PLAYING_URL = f"http://{NEW_HOST}:80/device-state/currently-playing"


async def _start_user_flow(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_user_flow_creates_entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Happy path: reachable controller becomes an entry titled by its name."""
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert not result["errors"]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Device Name"
    # Port is filled in from the schema default rather than typed.
    assert result["data"] == {CONF_HOST: HOST, CONF_PORT: 80}
    # Still the bare host, so entries predating the port field keep their id.
    assert result["result"].unique_id == HOST


async def test_user_flow_unreachable_controller(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """An unreachable host re-shows the form with cannot_connect."""
    aioclient_mock.get(SETTINGS_URL, exc=aiohttp.ClientConnectionError("no route"))

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_recovers_from_an_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A failed attempt re-shows the form; the next attempt in the same flow succeeds."""
    aioclient_mock.get(SETTINGS_URL, exc=aiohttp.ClientConnectionError("no route"))
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_unexpected_answer_is_unknown(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Something answered, but not like a controller: 'unknown', not 'cannot_connect'."""
    aioclient_mock.get(SETTINGS_URL, status=500)
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_title_falls_back_to_host(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A controller with no Bluetooth name is titled by its address, not left blank."""
    nameless = copy.deepcopy(HUB_SETTINGS_RESPONSE)
    nameless["state"]["reported"]["hubSettings"]["bluetoothName"] = "  "
    aioclient_mock.get(SETTINGS_URL, json=nameless)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == HOST


async def test_user_flow_rejects_duplicate_host(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The same controller cannot be added twice."""
    MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST).add_to_hass(hass)
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_updates_host_and_unique_id(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A DHCP change is an address edit, not a delete-and-re-add."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)
    aioclient_mock.get(NEW_SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(NEW_PLAYING_URL, json=COLOR_ON_RESPONSE)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: NEW_HOST})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == NEW_HOST
    assert entry.unique_id == NEW_HOST


async def test_reconfigure_recovers_from_an_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A typo in the new address re-shows the form; the corrected one goes through."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)
    aioclient_mock.get(NEW_SETTINGS_URL, exc=aiohttp.ClientConnectionError("no route"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: NEW_HOST})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(NEW_SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(NEW_PLAYING_URL, json=COLOR_ON_RESPONSE)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: NEW_HOST})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == NEW_HOST


async def test_upgrade_from_address_keyed_identity_keeps_entity_and_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An entry created by 0.4 has its light and device keyed on the address; they are re-keyed, not duplicated."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, HOST)})
    entity_registry.async_get_or_create("light", DOMAIN, HOST, config_entry=entry, suggested_object_id="device_name")
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    lights = [e for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id) if e.domain == "light"]
    assert [e.entity_id for e in lights] == ["light.device_name"]
    assert lights[0].unique_id == entry.entry_id
    assert hass.states.get("light.device_name").state == "on"
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(devices) == 1
    assert devices[0].identifiers == {(DOMAIN, entry.entry_id)}
    # Every companion landed on the same, re-keyed device.
    assert {e.device_id for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id)} == {devices[0].id}


async def test_reconfigure_keeps_entity_and_device(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Changing the address must not register a second entity or device.

    Identity is keyed on the entry id, not the host. Keyed on the host, every
    DHCP change would create ``light.device_name_2`` and a second device while
    orphaning the originals along with their history.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    before = sorted(e.entity_id for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    assert "light.device_name" in before

    aioclient_mock.get(NEW_SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(NEW_PLAYING_URL, json=COLOR_ON_RESPONSE)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: NEW_HOST})
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"

    after = sorted(e.entity_id for e in er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    assert after == before
    assert hass.states.get("light.device_name") is not None
    assert len(dr.async_entries_for_config_entry(device_registry, entry.entry_id)) == 1
    # The entry was reloaded onto the new address, not just re-saved with it.
    assert entry.runtime_data.client.host == NEW_HOST


async def test_user_flow_accepts_a_custom_port(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A non-default port is used for the request and stored on the entry."""
    aioclient_mock.get(f"http://{HOST}:8080/device-state/hub-settings", json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(f"http://{HOST}:8080/device-state/currently-playing", json=COLOR_ON_RESPONSE)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST, CONF_PORT: 8080})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: HOST, CONF_PORT: 8080}
    # The port joins the identity, so the same host on two ports can coexist.
    assert result["result"].unique_id == f"{HOST}:8080"


async def test_the_same_host_on_a_different_port_is_not_a_duplicate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Two simulators on one machine must both be addable."""
    MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST, CONF_PORT: 80}, unique_id=HOST).add_to_hass(hass)
    aioclient_mock.get(f"http://{HOST}:8080/device-state/hub-settings", json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(f"http://{HOST}:8080/device-state/currently-playing", json=COLOR_ON_RESPONSE)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST, CONF_PORT: 8080})

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_an_entry_without_a_port_still_sets_up(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Entries created before the port field existed need no migration."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("light.device_name") is not None


async def test_reconfigure_rejects_another_entrys_host(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Repointing onto a host another entry already owns aborts."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST)
    entry.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, data={CONF_HOST: NEW_HOST}, unique_id=NEW_HOST).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: NEW_HOST})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- discovery -----------------------------------------------------------------


def _device(serial: str = "ABC123", host: str = HOST, http_port: int = 80) -> SddpDevice:
    return SddpDevice(
        serial=serial,
        host=host,
        hostname=f"Gemstone-{serial}",
        kind="response",
        tran=1,
        max_age=1800,
        http_port=http_port,
        raw={},
    )


def _dhcp(ip: str = HOST, hostname: str = "gemstone-abc123") -> SimpleNamespace:
    # Home Assistant lower-cases the hostname before matching, and the real
    # DhcpServiceInfo cannot be imported without aiodhcpwatcher; the flow
    # reads only these three attributes.
    return SimpleNamespace(ip=ip, hostname=hostname, macaddress="aabbccddeeff")


async def _start_dhcp_flow(hass: HomeAssistant, info: SimpleNamespace) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_DHCP}, data=info)


async def test_dhcp_discovery_confirms_and_keys_the_entry_on_the_serial(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    result = await _start_dhcp_flow(hass, _dhcp())
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"] == {"name": "gemstone-abc123", "host": HOST}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Device Name"
    assert result["data"] == {CONF_HOST: HOST, CONF_PORT: 80, CONF_SERIAL: "abc123"}
    assert result["result"].unique_id == "abc123"


async def test_integration_discovery_confirms_with_the_controllers_own_name(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={CONF_HOST: HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123", CONF_NAME: "Gemstone-ABC123"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"] == {"name": "Gemstone-ABC123", "host": HOST}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The serial keeps the controller's capitalisation; the unique id is normalised.
    assert result["data"] == {CONF_HOST: HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123"}
    assert result["result"].unique_id == "abc123"


async def test_discovery_of_a_moved_controller_updates_its_address(hass: HomeAssistant) -> None:
    """A DHCP change is absorbed: the entry is updated, not duplicated, and no form is shown."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123"}, unique_id="abc123"
    )
    entry.add_to_hass(hass)

    result = await _start_dhcp_flow(hass, _dhcp(ip=NEW_HOST))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == NEW_HOST
    assert entry.data[CONF_SERIAL] == "ABC123"


async def test_discovery_adopts_an_entry_added_by_address(hass: HomeAssistant) -> None:
    """An entry made before discovery existed takes the serial instead of appearing as a new device."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id=HOST, title="Device Name")
    entry.add_to_hass(hass)

    result = await _start_dhcp_flow(hass, _dhcp())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == "abc123"
    assert entry.data == {CONF_HOST: HOST, CONF_SERIAL: "abc123"}


async def test_discovery_of_the_same_controller_twice_is_one_flow(hass: HomeAssistant) -> None:
    """SDDP and DHCP both report a controller; the second flow aborts, the first stays."""
    first = await _start_dhcp_flow(hass, _dhcp())
    assert first["type"] is FlowResultType.FORM

    second = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={CONF_HOST: HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123", CONF_NAME: "Gemstone-ABC123"},
    )
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_discovery_confirm_says_when_the_local_api_is_off(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Discovery proved the controller is there, so an HTTP failure means Allow Local Commands is off."""
    aioclient_mock.get(SETTINGS_URL, exc=aiohttp.ClientConnectionError("refused"))
    result = await _start_dhcp_flow(hass, _dhcp())
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "local_api_disabled"}

    # The server answers but reports the flag off: same advice.
    disabled = copy.deepcopy(HUB_SETTINGS_RESPONSE)
    disabled["state"]["reported"]["hubSettings"]["tcpEnabled"] = False
    aioclient_mock.clear_requests()
    aioclient_mock.get(SETTINGS_URL, json=disabled)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "local_api_disabled"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_dhcp_with_a_bare_prefix_is_not_a_controller(hass: HomeAssistant) -> None:
    result = await _start_dhcp_flow(hass, _dhcp(hostname="gemstone-"))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_gemstone"


async def test_user_flow_offers_the_controllers_a_search_finds(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    with patch("custom_components.gemstone_lights.discovery.async_discover", return_value={"abc123": _device()}):
        result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick_device"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DEVICE: "abc123"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Device Name"
    assert result["data"] == {CONF_HOST: HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123"}
    assert result["result"].unique_id == "abc123"


async def test_user_flow_pick_device_can_fall_through_to_an_address(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(NEW_SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(NEW_PLAYING_URL, json=COLOR_ON_RESPONSE)

    with patch("custom_components.gemstone_lights.discovery.async_discover", return_value={"abc123": _device()}):
        result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DEVICE: MANUAL})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: NEW_HOST})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: NEW_HOST, CONF_PORT: 80}
    assert result["result"].unique_id == NEW_HOST


async def test_user_flow_does_not_offer_a_configured_controller(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST, CONF_SERIAL: "ABC123"}, unique_id="abc123").add_to_hass(hass)
    with patch("custom_components.gemstone_lights.discovery.async_discover", return_value={"abc123": _device()}):
        result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_flow_learns_the_serial_when_the_controller_answers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A controller added by address is still keyed on its serial when it answers a unicast query."""
    aioclient_mock.get(SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(PLAYING_URL, json=COLOR_ON_RESPONSE)

    result = await _start_user_flow(hass)
    with patch("custom_components.gemstone_lights.discovery.async_query", return_value=_device()):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123"}
    assert result["result"].unique_id == "abc123"


async def test_user_flow_rejects_the_address_of_a_serial_keyed_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Typing the address of a discovered controller must not make a second entry, even if it is silent now."""
    MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123"}, unique_id="abc123"
    ).add_to_hass(hass)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_keeps_the_serial_identity(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123"}, unique_id="abc123"
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(NEW_SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)
    aioclient_mock.get(NEW_PLAYING_URL, json=COLOR_ON_RESPONSE)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: NEW_HOST})
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == "abc123"
    assert entry.data == {CONF_HOST: NEW_HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123"}


async def test_reconfigure_refuses_a_different_controller(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Pointing a serial-keyed entry at another controller would silently swap devices."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: HOST, CONF_PORT: 80, CONF_SERIAL: "ABC123"}, unique_id="abc123"
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(NEW_SETTINGS_URL, json=HUB_SETTINGS_RESPONSE)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    with patch(
        "custom_components.gemstone_lights.discovery.async_query", return_value=_device("XYZ789", host=NEW_HOST)
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: NEW_HOST})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data[CONF_HOST] == HOST
