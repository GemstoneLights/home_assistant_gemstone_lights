"""SDDP tests: the parser against the firmware's exact bytes, and the sockets against the simulator.

The wire shapes are copied from the firmware's ``sddp_api.c`` (the constant
fields, the quoted ``From``/``Host``, the CRLF lines), not from the
simulator, so the simulator's rendering is checked against them too
(test_hub2_sim.py) rather than only against itself.
"""

import asyncio
import socket

from custom_components.gemstone_lights import sddp
from custom_components.gemstone_lights.sddp import (
    SddpDevice,
    SddpListener,
    build_search_request,
    parse_sddp,
    serial_from_hostname,
)
from tools.hub2_sim import SDDP_ALIVE, Simulator, sddp_message, start_sddp

SOURCE = ("192.168.1.50", 1902)

FIRMWARE_RESPONSE = (
    b"SDDP/1.0 200 OK\r\n"
    b'From: "192.168.1.50:1902"\r\n'
    b'Host: "Gemstone-ABC123"\r\n'
    b"Tran: 1234\r\n"
    b"Max-Age: 1800\r\n"
    b'Type: "GemstoneLights:controller"\r\n'
    b'Proxies: "light"\r\n'
    b'Primary-Proxy: "light"\r\n'
    b'Manufacturer: "Gemstone Lights"\r\n'
    b'Model: "Gemstone Lights"\r\n'
    b'Driver: "gemstone_lights.c4z"\r\n'
)
_FIELDS = FIRMWARE_RESPONSE.split(b"\r\n", 1)[1].replace(b"Tran: 1234\r\n", b"")
FIRMWARE_ALIVE = b"NOTIFY ALIVE SDDP/1.0\r\n" + _FIELDS
FIRMWARE_OFFLINE = b"NOTIFY OFFLINE SDDP/1.0\r\n" + _FIELDS
FIRMWARE_IDENTIFY = b"NOTIFY IDENTIFY SDDP/1.0\r\n" + _FIELDS


# --- parsing -----------------------------------------------------------------


def test_search_response_parses_to_a_device() -> None:
    device = parse_sddp(FIRMWARE_RESPONSE, SOURCE)
    assert device == SddpDevice(
        serial="ABC123",
        host="192.168.1.50",
        hostname="Gemstone-ABC123",
        kind="response",
        tran=1234,
        max_age=1800,
        http_port=80,
        raw=device.raw,
    )
    # Quotes are stripped and keys lower-cased, so the raw headers are usable too.
    assert device.raw["type"] == "GemstoneLights:controller"
    assert device.raw["driver"] == "gemstone_lights.c4z"


def test_notifies_parse_with_their_kind() -> None:
    assert parse_sddp(FIRMWARE_ALIVE, SOURCE).kind == "alive"
    assert parse_sddp(FIRMWARE_OFFLINE, SOURCE).kind == "offline"
    assert parse_sddp(FIRMWARE_IDENTIFY, SOURCE).kind == "identify"
    assert parse_sddp(FIRMWARE_ALIVE, SOURCE).tran is None


def test_lf_only_lines_and_a_trailing_nul_are_tolerated() -> None:
    assert parse_sddp(FIRMWARE_RESPONSE.replace(b"\r\n", b"\n"), SOURCE).serial == "ABC123"
    assert parse_sddp(FIRMWARE_RESPONSE + b"\x00", SOURCE).serial == "ABC123"


def test_the_datagram_source_is_the_address_not_the_from_header() -> None:
    """The reply's From is what the controller believes; the source is what routed."""
    assert parse_sddp(FIRMWARE_RESPONSE, ("10.0.0.5", 1902)).host == "10.0.0.5"


def test_search_requests_are_not_devices() -> None:
    assert parse_sddp(build_search_request("192.168.1.10", 1), SOURCE) is None
    assert parse_sddp(b"SEARCH * SDDP/1.0\r\nHost: 192.168.1.10:5000\r\n", SOURCE) is None


def test_other_vendors_and_other_hostnames_are_not_devices() -> None:
    other_type = FIRMWARE_RESPONSE.replace(b'"GemstoneLights:controller"', b'"Sonos:ZonePlayer"')
    assert parse_sddp(other_type, SOURCE) is None
    other_host = FIRMWARE_RESPONSE.replace(b'"Gemstone-ABC123"', b'"living-room-tv"')
    assert parse_sddp(other_host, SOURCE) is None
    no_serial = FIRMWARE_RESPONSE.replace(b'"Gemstone-ABC123"', b'"Gemstone-"')
    assert parse_sddp(no_serial, SOURCE) is None
    assert parse_sddp(b"HTTP/1.1 200 OK\r\n", SOURCE) is None
    assert parse_sddp(b"", SOURCE) is None


def test_the_http_port_extension_is_read_and_defaults_to_80() -> None:
    assert parse_sddp(FIRMWARE_RESPONSE + b"Http-Port: 8080\r\n", SOURCE).http_port == 8080
    assert parse_sddp(FIRMWARE_RESPONSE + b"Http-Port: eighty\r\n", SOURCE).http_port == 80


def test_serial_from_hostname() -> None:
    assert serial_from_hostname("Gemstone-ABC123") == "ABC123"
    assert serial_from_hostname("gemstone-abc123") == "abc123"
    assert serial_from_hostname("Gemstone-") is None
    assert serial_from_hostname("hub2-ABC123") is None


def test_search_request_is_what_the_firmware_parser_accepts() -> None:
    """Quoted From, numeric Tran and Timeout, CRLF: SDDPHandleSearchRequest wants all three."""
    assert build_search_request("192.168.1.10", 1234, timeout=2) == (
        b'SEARCH * SDDP/1.0\r\nFrom: "192.168.1.10"\r\nTran: 1234\r\nTimeout: 2\r\n\r\n'
    )
    assert b"Timeout: 1\r\n" in build_search_request("192.168.1.10", 1, timeout=0)


# --- sockets, against the simulator on loopback -------------------------------


async def _responder(*sims: Simulator) -> tuple[asyncio.DatagramTransport, int]:
    transport, _ = await start_sddp(list(sims), "127.0.0.1", 0)
    return transport, transport.get_extra_info("sockname")[1]


async def test_query_gets_the_controllers_reply(socket_enabled: None) -> None:
    sim = Simulator("color-on", serial="ABC123")
    sim.http_port = 8080
    transport, port = await _responder(sim)
    try:
        device = await sddp.async_query("127.0.0.1", "127.0.0.1", port=port, wait=2.0)
    finally:
        transport.close()
    assert device is not None
    assert device.serial == "ABC123"
    assert device.hostname == "Gemstone-ABC123"
    assert device.host == "127.0.0.1"
    assert device.http_port == 8080
    assert device.kind == "response"


async def test_query_gives_up_when_nothing_answers(socket_enabled: None) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert await sddp.async_query("127.0.0.1", "127.0.0.1", port=port, wait=0.3) is None


async def test_search_collects_every_controller_once(socket_enabled: None) -> None:
    front = Simulator("color-on", serial="ABC123")
    garage = Simulator("color-on", serial="XYZ789")
    transport, port = await _responder(front, garage)
    try:
        # The same target twice: a controller reachable two ways is still listed once.
        found = await sddp.async_search(["127.0.0.1"], [("127.0.0.1", port), ("127.0.0.1", port)], wait=0.5)
    finally:
        transport.close()
    assert sorted(found) == ["abc123", "xyz789"]
    assert found["abc123"].serial == "ABC123"


async def test_search_with_no_usable_source_finds_nothing(socket_enabled: None) -> None:
    # An address this host does not own cannot be bound; the search skips it.
    assert await sddp.async_search(["192.0.2.1"], [("127.0.0.1", 1902)], wait=0.1) == {}


async def test_listener_hears_an_announcement(socket_enabled: None) -> None:
    heard: list[SddpDevice] = []
    announced = asyncio.Event()

    def on_device(device: SddpDevice) -> None:
        heard.append(device)
        announced.set()

    listener = SddpListener(on_device, source_ips=["127.0.0.1"], port=0)
    assert await listener.async_start()
    try:
        assert listener.port
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(sddp_message(SDDP_ALIVE, "ABC123", "127.0.0.1"), ("127.0.0.1", listener.port))
            sender.sendto(build_search_request("127.0.0.1", 1), ("127.0.0.1", listener.port))
        finally:
            sender.close()
        await asyncio.wait_for(announced.wait(), 2.0)
        await asyncio.sleep(0.1)
    finally:
        listener.close()
    assert [(device.kind, device.serial) for device in heard] == [("alive", "ABC123")]
    assert listener.port is None
