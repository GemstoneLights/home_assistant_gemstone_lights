# Documentation

Start with the [README](../README.md) for installing and using the integration.
This directory holds the rest:

| Document | Read it for |
|---|---|
| [design.md](design.md) | How the integration is built and **why** each decision was made; what changed in 0.5.0; what is still unverified; how to extend it; the [roadmap](design.md#8-roadmap) of what is left to do and what gates it. |

The Hub2 local HTTP API is documented by Gemstone. That documentation is not
redistributed here; [design.md](design.md#2-the-hub-for-people-who-know-home-assistant)
describes everything the integration relies on.

## Behaviour confirmed on hardware, beyond the documentation

- **`origin` echo.** Every response carries an `origin` property. If the request
  body set one, the response echoes the same value; otherwise responses say
  `origin: "http"`. Any value is accepted, so the component sends
  `"homeassistant"`.
- **`colorB` is local-HTTP only.** Over MQTT the controller still reports the
  legacy packed `color` integer.
- **No `txId` on the local API**, and no statement on whether a `POST` reply
  reflects applied or merely accepted state. Settle on hardware before relying
  on the reply beyond best-effort.

## From the firmware source, not yet confirmed on hardware

- **SDDP.** The controller answers `SEARCH * SDDP/1.0` on UDP 1902 only when the
  request has a quoted `From`, a `Tran` and a `Timeout`; the reply is unicast to
  the `From` address at the request's source port. It multicasts `NOTIFY ALIVE`
  to `239.255.255.250:1902` every 300 s and on every cloud reconnect, and the
  first one after a power cycle comes at about five minutes of uptime. The
  cloud can make it send `NOTIFY IDENTIFY`, `ALIVE` or `OFFLINE` on demand
  (shadow property `control4`); the app's Control4 Identify button is the
  `IDENTIFY` one.
- **Ethernet drops multicast searches.** The socket never joins the group, so a
  search must arrive as broadcast or unicast. Whether the Wi-Fi module delivers
  broadcast or multicast searches to the controller is unknown; the NOTIFYs it
  sends do not depend on that.
- **Nothing local works without the cloud.** The UDP and HTTP servers open only
  once the controller is connected to AWS.

## Assumed by the simulator, not confirmed

- **Out-of-range values are answered with 400.** The documented 400 cases are
  malformed JSON and a missing body. `tools/hub2_sim.py` also rejects values
  outside the documented bounds, so a client bug fails loudly on the bench;
  whether the firmware rejects, clamps or ignores them is unknown.
- **Pixel numbering in `architectural.staticColors[].lights`** -- 0- or
  1-based, per output or across the controller, and what unlisted pixels do.
  The documented example uses 4, 6, 8... and never 0.
- **`direction` above 5.** The API document bounds the field at 1-10, but the
  animation specification defines only six directions and no animation reads
  anything above 5. The simulator follows the animations and rejects the rest;
  whether the firmware would accept 7 is unknown and no longer interesting.
- **`extraParameters` shape.** The simulator checks that the object is
  well-formed -- a version, numeric indexes, whole values -- but not which
  indexes belong to which animation. That check lives in the integration's
  own tests, so the simulator stays an independent reader of the wire format.
- **413's buffer size.** "Payload exceeds buffer size" is documented; the size
  is not. The simulator assumes 8 KiB.
- **The scene survives a power cycle.** The controller has schedules and
  restores what it was showing, so the simulator keeps the scene across
  `/_sim/power-cycle` and `/_sim/reboot`; unverified.
- **`playlist` and `impulse` shapes.** Beyond `lengthInSeconds`, nothing is
  defined. The simulator's profiles carry that field and a name only.
- **`clamp` validation mode is a hypothesis.** The simulator can clamp
  out-of-range values into bounds instead of answering 400, so both readings
  of the firmware can be rehearsed; neither has been observed.

The full list, and what each one would unblock, is in
[design.md](design.md#6-unverified-behaviour).

The API is a work in progress and its endpoints may change, which is why
`api.py` parses defensively rather than asserting a fixed shape.
