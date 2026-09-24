"""Find controllers on the network and hand them to the config flow.

Three routes lead to the flow's discovery steps: an SDDP search out of every
enabled interface at start-up and every fifteen minutes, an SDDP listener that
hears the announcements controllers make on their own, and Home Assistant's
DHCP watcher matching the ``gemstone-*`` hostname (manifest.json). sddp.py
speaks the protocol and knows nothing of Home Assistant; this module knows
Home Assistant's network configuration and its flow dispatcher, and nothing of
the wire format.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable

from homeassistant.components import network
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import discovery_flow

from . import sddp
from .const import CONF_SERIAL, DOMAIN, SDDP_QUERY_TIMEOUT, SDDP_SEARCH_TIMEOUT
from .sddp import SddpDevice, SddpListener

_LOGGER = logging.getLogger(__name__)


async def async_source_ips(hass: HomeAssistant) -> list[str]:
    """Return the IPv4 addresses Home Assistant is configured to speak from."""
    return [
        str(address)
        for address in await network.async_get_enabled_source_ips(hass)
        if address.version == 4 and not address.is_loopback
    ]


async def async_discover(hass: HomeAssistant) -> dict[str, SddpDevice]:
    """Search every enabled interface; controllers keyed by lower-cased serial.

    The search goes to the multicast group and to every broadcast address,
    because a controller on Ethernet only hears the broadcast (sddp.py).
    """
    source_ips = await async_source_ips(hass)
    if not source_ips:
        return {}
    targets = [(sddp.SDDP_GROUP, sddp.SDDP_PORT)]
    targets.extend((str(address), sddp.SDDP_PORT) for address in await network.async_get_ipv4_broadcast_addresses(hass))
    found = await sddp.async_search(source_ips, targets, wait=SDDP_SEARCH_TIMEOUT)
    if found:
        _LOGGER.debug("SDDP search found %s", ", ".join(f"{d.hostname} at {d.host}" for d in found.values()))
    return found


async def async_query(hass: HomeAssistant, host: str) -> SddpDevice | None:
    """Ask the controller at one address who it is; None if it does not answer.

    Only for an IP literal: a hostname would have to be resolved on the event
    loop, and every documented setup uses the address.
    """
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return None
    source_ip = await network.async_get_source_ip(hass, target_ip=host)
    return await sddp.async_query(host, source_ip, wait=SDDP_QUERY_TIMEOUT)


@callback
def async_trigger_discovery(hass: HomeAssistant, devices: Iterable[SddpDevice]) -> None:
    """Start a discovery flow for each controller; the flow drops duplicates by serial."""
    for device in devices:
        discovery_flow.async_create_flow(
            hass,
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={
                CONF_HOST: device.host,
                CONF_PORT: device.http_port,
                CONF_SERIAL: device.serial,
                CONF_NAME: device.hostname,
            },
        )


@callback
def async_handle_announcement(hass: HomeAssistant, device: SddpDevice) -> None:
    """Turn a controller's own NOTIFY into a discovery flow.

    ``alive`` is the five-minute heartbeat and the cloud-reconnect announcement.
    ``identify`` is what the Gemstone app's Control4 Identify button sends (it
    asks the cloud to have the controller announce itself), so it is the way
    to make a controller appear at once instead of within five minutes.
    ``offline`` says nothing about where a controller is, so it starts nothing.
    """
    if device.kind in ("alive", "identify"):
        async_trigger_discovery(hass, [device])


async def async_start_listener(hass: HomeAssistant) -> None:
    """Listen for controllers' own announcements until Home Assistant stops."""

    @callback
    def announced(device: SddpDevice) -> None:
        async_handle_announcement(hass, device)

    listener = SddpListener(announced, source_ips=await async_source_ips(hass))
    if not await listener.async_start():
        return

    @callback
    def stop(_: Event) -> None:
        listener.close()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
