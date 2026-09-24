"""The Gemstone Lights integration.

Connects Home Assistant to a Hub2 controller over its LAN-local HTTP API.
AWS and the Gemstone cloud are not involved: the component is a direct client
of the controller, so it keeps working during an internet outage.

Controllers are found on the network by the Control4 discovery protocol the
firmware already speaks (sddp.py, discovery.py) and by their DHCP hostname
(manifest.json), so setup is normally a confirmation rather than an address.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from . import discovery
from .api import DEFAULT_PORT, GemstoneClient, GemstoneError
from .const import (
    CONF_SERIAL,
    DISCOVERY_INTERVAL,
    DOMAIN,
    unique_id_for,
    unique_id_for_serial,
)
from .coordinator import GemstoneCoordinator
from .store import SavedPatterns

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]

# Everything is set up through config entries; YAML would only be a mistake.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type GemstoneConfigEntry = ConfigEntry[GemstoneCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Start looking for controllers before any entry exists.

    A search now and every fifteen minutes, and a listener for the
    announcements controllers make on their own. Found controllers surface
    as Discovered cards; the config flow drops the ones already configured.
    """

    async def _async_discovery(*_: Any) -> None:
        discovery.async_trigger_discovery(hass, (await discovery.async_discover(hass)).values())

    hass.async_create_background_task(_async_discovery(), "gemstone_lights first discovery", eager_start=True)
    async_track_time_interval(hass, _async_discovery, DISCOVERY_INTERVAL, cancel_on_shutdown=True)
    await discovery.async_start_listener(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GemstoneConfigEntry) -> bool:
    """Connect to the controller and start polling."""
    # Entries created before the port field existed carry no port; they are all
    # on 80, which is what firmware serves.
    client = GemstoneClient(
        entry.data[CONF_HOST],
        async_get_clientsession(hass),
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
    )
    try:
        hub_settings = await client.async_get_hub_settings()
    except GemstoneError as err:
        raise ConfigEntryNotReady(f"Cannot read hub settings from {client.host}: {err}") from err

    saved_patterns = SavedPatterns(hass, entry.entry_id)
    await saved_patterns.async_load()
    coordinator = GemstoneCoordinator(hass, entry, client, hub_settings, saved_patterns)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await _async_migrate_identity(hass, entry)
    _async_remove_stale_entities(hass, entry)
    if CONF_SERIAL not in entry.data:
        entry.async_create_background_task(hass, _async_learn_serial(hass, entry), "gemstone_lights learn serial")
    # Options (the idle poll interval) apply via a clean reload. From Home
    # Assistant 2025.8 the options flow does that itself (config_flow.py), and
    # a listener here would make it raise.
    if not hasattr(config_entries, "OptionsFlowWithReload"):
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_migrate_identity(hass: HomeAssistant, entry: GemstoneConfigEntry) -> None:
    """Re-key entities and the device from the controller's address to the entry id.

    Versions before 0.5 used the address as entity and device identity, so a
    reconfigured address registered a second light and orphaned the first.
    Identity is now the entry id (entity.py). Without this, upgrading would do
    exactly what the change was meant to stop: the light registered under the
    old id would be left behind and a `_2` created beside it. Matches nothing
    on an entry created by 0.5 or later, so it is harmless afterwards.
    """
    host: str = entry.data[CONF_HOST]
    old_ids = {entry.unique_id, unique_id_for(host, entry.data.get(CONF_PORT, DEFAULT_PORT))} - {None}

    @callback
    def migrate(registry_entry: er.RegistryEntry) -> dict[str, str] | None:
        if registry_entry.unique_id in old_ids:
            return {"new_unique_id": entry.entry_id}
        return None

    await er.async_migrate_entries(hass, entry.entry_id, migrate)

    # Searched among this entry's own devices: identifiers are no longer unique
    # across config entries, which is why async_get_device(identifiers=...) is
    # deprecated (it stops working in Home Assistant 2027.8).
    device_registry = dr.async_get(hass)
    old_identifiers = {(DOMAIN, old_id) for old_id in old_ids}
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if device.identifiers & old_identifiers:
            device_registry.async_update_device(device.id, new_identifiers={(DOMAIN, entry.entry_id)})


@callback
def _async_remove_stale_entities(hass: HomeAssistant, entry: GemstoneConfigEntry) -> None:
    """Drop entities from platforms this integration no longer sets up.

    Home Assistant keeps a registry entry when its platform disappears, so it
    would sit on the device page for good, unavailable and unexplained. 0.6
    development builds gave each saved pattern a scene of its own (4.11) and put
    the animations in an Animation select before they went back to the light's
    effect list (4.7); anyone who ran one has those entries.

    Matched against PLATFORMS rather than a list of the shapes that went, so it
    keeps working if the platforms ever change again.
    """
    registry = er.async_get(hass)
    supported = {str(platform) for platform in PLATFORMS}
    # The Animation picker was a select, and select is still a platform here, so
    # its leftover needs naming rather than matching by domain.
    dropped = {f"{entry.entry_id}_animation", f"{entry.entry_id}_effect_direction"}
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.domain not in supported or registry_entry.unique_id in dropped:
            _LOGGER.debug("Removing %s: this integration no longer has that platform", registry_entry.entity_id)
            registry.async_remove(registry_entry.entity_id)


async def _async_learn_serial(hass: HomeAssistant, entry: GemstoneConfigEntry) -> None:
    """Re-key an address-keyed entry onto the controller's serial, if it will say.

    Entries made before 0.6, or by hand while the controller was not
    answering SDDP, are keyed on their address. Once the controller answers a
    unicast query the entry takes the serial, after which a DHCP change is
    absorbed by discovery instead of needing the reconfigure step. Silent
    when the controller does not answer: nothing about the entry is wrong.
    """
    device = await discovery.async_query(hass, entry.data[CONF_HOST])
    if device is None:
        return
    unique_id = unique_id_for_serial(device.serial)
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id != entry.entry_id and other.unique_id == unique_id:
            _LOGGER.debug("Not adopting serial %s for %s: another entry owns it", device.serial, entry.title)
            return
    hass.config_entries.async_update_entry(entry, unique_id=unique_id, data={**entry.data, CONF_SERIAL: device.serial})


async def _async_options_updated(hass: HomeAssistant, entry: GemstoneConfigEntry) -> None:
    """Reload the entry so the coordinator picks up the new interval."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: GemstoneConfigEntry) -> bool:
    """Stop polling and remove the platform."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
