# Design

How the integration works and why. Written for people who know Home Assistant
but not Gemstone controllers. Installing and testing are covered in the
[README](../README.md).

Gemstone documents the Hub2 local API, but that document isn't redistributed
here. Where this file says "the document", it means that one.

## 1. What it provides

One config entry is one controller, shown as one device:

| Entity or action | Purpose |
|---|---|
| `light` | On/off, brightness, RGBW colour, and the 29 animations as effects |
| `number` | Animation speed (1–255) |
| `sensor` × 3 | Pixel count, with per-output counts as attributes; SPI and Wi-Fi firmware (disabled by default) |
| `binary_sensor` | TCP enabled, the controller's local-API flag (disabled by default) |
| `select` | Pattern: the patterns saved here, replayed whole |
| `gemstone_lights.play_pattern` | An animation with every parameter the API accepts |
| `gemstone_lights.save_playing`, `forget_pattern` | Keep the playing pattern for later, or drop one |

## 2. The controller

Hub2 drives up to 4,096 RGBW pixels, with a real warm-white channel, across four
outputs. It has Wi-Fi and Bluetooth. Hub1 is an unrelated Tuya device with no
local API.

The local API is plain HTTP on port 80 with no authentication. It's enabled per
controller with Allow Local Commands in the app, or over Bluetooth with `B801`
(`B800` turns it off and needs a power cycle). There are three routes:

| Route | Purpose |
|---|---|
| `GET /device-state/hub-settings` | Pixel counts per output, firmware versions, Bluetooth name, location, local-API flag |
| `GET /device-state/currently-playing` | Power, plus exactly one scene: a colour, a pattern or an architectural design |
| `POST /device-control/play` | Sets the scene or just the power, and replies with the reported state |

Things the routes don't tell you:

- Requests and replies are AWS IoT shadow documents (`state.desired` and
  `state.reported`). `api.py` wraps and unwraps them.
- Colours are 32-bit integers with red in the low byte:
  `(warm << 24) + (blue << 16) + (green << 8) + red`. Pure red is `255`. Only
  `rgbw_to_int` and `parse_rgbw_int` deal with this.
- The local API uses `colorB: {value, brightness}`. The cloud, MQTT and the app
  still use the older `color` integer with brightness baked in, so the parser
  separates the two and a colour isn't dimmed twice.
- A play command sets one of `colorB`, `pattern` or `architectural` and sends
  the others as `null`. `playlist` and `impulse` appear in the shape but aren't
  defined (Q7), so the integration never sends them.
- A power-only command (`{"onState": true}`) gets a reply containing only
  `onState` (document section 3.4).
- 503 means busy. The HTTP server runs on the same chip that drives the pixels.
- The controller can't push changes, so state is polled.
- Confirmed on hardware: every response carries an `origin`, echoing the
  request's value or `"http"` when there isn't one. The integration sends
  `"homeassistant"`. There's no `txId`.
- The controller announces itself. It speaks Control4's SDDP for the Control4
  driver: `NOTIFY ALIVE` to `239.255.255.250:1902` every five minutes and on
  every cloud reconnect, and an answer to `SEARCH *` on UDP 1902. Both carry
  `Host: "Gemstone-<serial>"`, and the same string is its DHCP hostname. That
  is what discovery uses (4.15). The UDP server only opens once the
  controller has connected to the cloud, so a controller with no internet is
  not discoverable, and the first announcement after a power cycle comes at
  about five minutes of uptime.
- The API is still changing, so `api.py` parses defensively instead of
  expecting a fixed shape.

## 3. Modules

| Module | Responsible for |
|---|---|
| `api.py` | The protocol: colour encoding, shadow documents, parsing into `HubSettings` and `CurrentlyPlaying`, command builders with the document's bounds, and `GemstoneClient` (one lock, one retry on 503). Imports nothing from Home Assistant. |
| `coordinator.py` | Polling, adopting command replies as state, animation speed, re-reading hub settings |
| `entity.py` | `GemstoneEntity`: identity, `DeviceInfo`, and `_async_send`, the one path every command takes |
| `light.py` | The light entity and both actions |
| `number.py`, `sensor.py`, `binary_sensor.py` | The companion entities, each an `EntityDescription` with a `value_fn` |
| `select.py` | The Pattern control |
| `services.py`, `services.yaml` | Action schemas and their descriptions |
| `palette.py` | The app's swatches: what Home Assistant shows, what the controller is sent, and the lookup each way. Imports only `RGBW` from `api.py`. |
| `store.py` | Patterns kept by Home Assistant, in its own storage |
| `animations.py` | The 29 animations and what a pattern must carry for each: directions, background colour, extra parameters. Imports nothing from Home Assistant. |
| `sddp.py` | The Control4 discovery protocol as a client: the SEARCH the firmware accepts, a parser for its replies and NOTIFYs, a search, a unicast query, and a listener. Imports nothing from Home Assistant. |
| `discovery.py` | Runs the search at start-up and every fifteen minutes, keeps the listener open, and turns what they hear into discovery flows |
| `config_flow.py` | Discovery confirm, pick-from-search, setup by address, reconfigure and options |
| `tools/hub2_sim.py` | A fake controller on a real socket, with fault injection |
| `tools/dev_ha.sh`, `tools/make_bench_dashboard.py` | A local Home Assistant for development, and a dashboard with a button for every capability |

State flows one way: a poll or a command reply becomes a `CurrentlyPlaying`, the
coordinator publishes it, and the entities read it.

## 4. Decisions

### 4.1 `api.py` doesn't import Home Assistant

It's tested with plain pytest against the document's payloads. Home Assistant
core also requires an integration's API client to be a separate PyPI package,
so keeping it independent makes publishing it later a move, not a rewrite.

### 4.2 Polling adapts to activity

The controller is polled every 5 seconds for a minute after any activity,
every idle interval (30 seconds by default) otherwise, and less often while it's
unreachable, doubling up to 120 seconds (`next_interval`). A change made
somewhere else, like the app or a schedule, counts as activity. A fixed interval
would be too slow for a dashboard or too busy for an idle controller.

### 4.3 Command replies are adopted as state

`POST /device-control/play` replies with the reported state, so
`GemstoneCoordinator.async_apply_result` adopts it instead of polling again.
It's the only place that trusts a reply, because it isn't confirmed whether a
reply means the change was applied or only accepted (Q1). If it turns out to be
only accepted, this one method changes to schedule a short refresh.

A power-only reply has no scene, so `async_apply_result` keeps the previous
colour, brightness, pattern and design under the new power state. Taking the
reply literally blanked the light card until the next poll, and a brightness
change in that gap replaced a running pattern with a plain colour.

### 4.4 Identity is the config entry id

The config entry's unique id is the controller's serial number when discovery
has learnt it (4.15), and its IP address otherwise, as every entry was before
0.6. Either stops the same controller being added twice. Entities and the
device are keyed on `entry.entry_id` instead, so changing the address with
Reconfigure, or having discovery change it, keeps the same entity, device and
history. Version 0.4 keyed them on the address, and `_async_migrate_identity`
re-keys those entries on first load. An address-keyed entry moves to the serial
the first time the controller answers a unicast query at setup, or the first
time discovery sees it; nothing about the entry changes but its key.

### 4.5 One light, and no zones

Every play command targets the whole controller. The only per-pixel control is
`architectural.staticColors`, a colour for each list of pixel numbers, and this
integration does not expose it. A `play_architectural` action did, until it was
removed as unusable: nobody knows their pixel numbers, and whether numbering
starts at 0 or 1, counts per output or across the controller, is still
unverified (Q3). The local API also can't list or target the zones set up in the
app (Q6), so there is nothing to build zone entities from either.

A design played from the app is still handled: it parses, the light shows its
first colour, and a brightness change replays it rather than replacing it with a
plain colour. Nothing here sends a new one, so the builder that did went with
the action; `api.build_architectural_replay_command` stays, because that
brightness path calls it.

### 4.6 Animation speed lives on the coordinator

A Home Assistant effect is just a name, so an animation's speed needs its own
`number` entity, the same approach WLED takes. It is called Animation speed,
which is the ecosystem's word; the internal key stays `effect_speed`, so no
existing entity is orphaned by the naming. The value lives on the coordinator
because the light and `play_pattern` both read it. Storing it in `entry.options`
would reload the entry on every change. The range is 1–255: zero is outside what
the animations accept.

A pattern reported by the controller always wins. Otherwise the last value set
in Home Assistant wins, and a value restored after a restart only applies if the
controller reported nothing at startup. Moving the slider while a pattern plays
replays it with the new value. While the lights are off it just stores the
value, since replaying would switch them on.

Direction had a number entity beside it and no longer does (4.17): it is not one
value the user holds, it is a property of whichever animation is playing, and
thirteen of the twenty-nine take none at all.

### 4.7 Animations on the light, saved patterns in one select

A `pattern` on the wire is a palette *and* an animation at once, so the two
combine rather than replace each other. They are shown in the two places Home
Assistant already has for them:

| | Where | Picking one |
|---|---|---|
| The 29 animations | `light.effect_list`, with `EFFECT_OFF` first | Changes the animation, keeps the palette |
| Saved patterns | a `select` named Pattern, no entity category | Replays it whole, animation included |

Together they read as what is playing: the light's effect says `Fireworks`,
Pattern says `Canada Day`, and that is Fireworks running on Canada Day's colours.
Pattern matches by name while a saved pattern plays untouched; once the effect
changes, the controller reports the new animation's name, so the palette is what
identifies it instead. Two patterns saved with identical colours cannot be told
apart that way -- the first wins, which is the cost of storing nothing on the
controller.

This shape is the ecosystem's, not ours. The developer documentation for
`select` names the alternative as the anti-pattern in as many words: *"a bulb can
have user selectable light effects. While that could be done using this select
entity, it should really be part of the light entity, which already supports
light effects."* And the core survey is one-sided -- wled, hue, nanoleaf, twinkly,
lifx, flux_led, yeelight, esphome, wiz, tplink and hyperion all put firmware
animations in `effect_list`, and none uses a select for them. WLED is the closest
analogue and splits exactly this way: firmware effects on the light, its stored
Presets and Playlists as selects with no entity category.

Declaring `LightEntityFeature.EFFECT` is also what buys the integration, in core:
Google Assistant's Modes trait (the only assistant that can set an effect at
all), a Home Assistant scene's ability to snapshot and restore which animation
was playing (`light/reproduce_state.py` groups `ATTR_EFFECT`), light-group effect
merging, the `effect:` field in the service form, the automation attribute
dropdown, and from HA 2026.9 the light-effect tile feature. None of that is
reachable from a `select`.

`EFFECT_OFF` leads the list because core asks a light with effects to report it
when nothing is rendered. The controller has no stop command, so picking it
plays a plain colour -- the power toggle would leave the pattern running (3.4).

Saved patterns stay a select rather than joining that list, and rather than
becoming a scene each. A select is where core puts a stored, user-authored bundle;
it can report *which* pattern is playing, which a stateless `scene` cannot; and it
holds at one row however many are saved. The cost, stated plainly: a select option
is not addressable by voice, and nothing else can be either -- Home Assistant's
own light intent has no `effect` slot, so speaking an animation was never on the
table. Only a scene per pattern would have been speakable, and that trades one row
for one per pattern.

Earlier arrangements, and why each fell: patterns prefixed into the effect list
(one list reading as two things); a scene per pattern (an entity each, filling the
device page); both merged into one select (the single value could only show one of
them); animations in their own select (the same catalogue twice, and the
documented anti-pattern); a custom tile-card feature with a dialog of our own
(dashboard only, and a JavaScript module served to every dashboard in the
instance).

Home Assistant keeps a registry entry when its platform disappears, so entities
from those shapes would sit on the device page unavailable and unexplained.
`_async_remove_stale_entities` drops any of this entry's entities whose domain is
not one of `PLATFORMS`, plus the Animation picker's own id, which a domain rule
would spare now that select is a platform again.

### 4.8 Hub settings refresh after an outage and hourly

Firmware versions and pixel counts only change across a reboot, and a reboot
looks like an outage from here. The coordinator re-reads `hub-settings` on the
first successful poll after a failure and once an hour, and keeps the old values
if that read fails.

### 4.9 Diagnostic entities

Pixel count is a single sensor with the four outputs as attributes, since most
installs use one output. It stays enabled because it is the one thing that says
how long the run is, which is what someone reaching for pixel numbers needs. Firmware versions and the TCP flag start disabled. The
TCP flag has no device class, because `connectivity` would present a feature
flag as a network connection. `exists_fn` skips an entity when older firmware
leaves its field out.

### 4.10 Actions

Three, all entity services registered by their platform: `play_pattern` and
`save_playing` and `forget_pattern`, all on the light. Their
schemas are plain dicts, because `vol.Schema` is deprecated for entity services,
and use the document's bounds, the same ones `api.py` enforces, so anything that
passes the schema can be built. `animation` accepts a slug or a display name.
Colour fields use the `object` selector because Home Assistant has no RGBW colour
selector. The `animation` options in `services.yaml` come from `const.ANIMATIONS`,
and a test fails if they drift apart.

The surface is deliberately small: patterns and colours are what someone can
actually set from here. Two actions were removed rather than kept for
completeness -- `play_architectural` (4.5), and a `copy_playing` that returned
the running scene as a pasteable action step, which saving it (4.11) does in one
action instead.

### 4.11 Home Assistant keeps the saved patterns, because the controller cannot

The app builds patterns this integration cannot: twenty colours, its own names,
parameters the play command takes but no Home Assistant control exposes.
Someone who likes one wants it back next week. The controller cannot help --
the local API plays a scene and reports the one playing, with no route that
saves a pattern and none that lists what the app has saved (Q6) -- so
`store.py` keeps it here, in Home Assistant's own storage, one store per
controller.

What is stored is the controller's own reported object, replayed verbatim
through `build_pattern_replay_command`, so a replay carries every field,
including the app's `referencePatternId`, which a rebuilt command would drop.

They are one `select` (4.7) rather than an entity each. A scene per pattern was tried first, following Hue: it activates in
a tap and suits automations, but it fills the device page and every dashboard
with an entity per pattern. The cost of one control, stated plainly: no
per-pattern entity for a voice assistant to activate directly, and forgetting
one names it rather than pointing at it.

Writing scripts for the user was rejected too: an integration editing someone's
configuration is not its place.

Saving under a name already used replaces that pattern, because two entries with
one name cannot be told apart. The light saves and forgets, and writes its own
state when the saved list changes, since neither goes through the coordinator's
polling.

Because "scene" is Home Assistant's word for an entity type, the light's own
attributes avoid it: they are `playing` and `playing_name`. The protocol
sections still say scene, which is the API's word for what is playing and is
right there.

### 4.12 The picker holds the colour that looks right; the boundary translates

Home Assistant's default favourites for an RGBW light are four warm whites,
which are not the colours this ecosystem uses, so the light seeds the app's own
swatches into the entity's registry options when it is first added: the three
whites, black and the nine colours, in the app's order. They are written once,
and only when the entity has no favourites of its own, so an edited row survives
a restart and so does an emptied one.

The controller's values cannot be used as they are. Home Assistant paints a
swatch from the payload it will send -- the frontend reads it off the payload's
one colour key, and `light.turn_on` marks every colour key mutually exclusive --
so a favourite cannot show one colour and send another. Left raw, the LED-tuned
values draw wrong: Orange reads red, Yellow reads orange, and the three whites
flatten into one, a screen having three channels where the roofline has four.
The app hides this in its own picker with display-only fills (`buildFillColor`),
which Home Assistant has no equivalent of.

So `palette.py` holds both values for each swatch and the light translates at
the three places a colour crosses: the `rgbw_color` it reports, the colour
`turn_on` was given, and `play_pattern`'s colours and background. Replay paths
stay raw, because
`playing.rgbw` and a reported pattern's colours are already controller values.

| Swatch | Home Assistant holds | Controller gets |
|---|---|---|
| Warm White | 253, 244, 188, 0 | 0, 0, 0, 255 |
| Cool White | 244, 253, 255, 0 | 255, 255, 255, 0 |
| Bright White | 255, 255, 255, 0 | 255, 255, 255, 255 |
| Orange | 255, 165, 0, 0 | 255, 30, 0, 0 |
| Yellow | 255, 255, 0, 0 | 255, 87, 0, 0 |
| Pink | 255, 105, 180, 0 | 255, 0, 122, 0 |
| Off, Red, Green, Aqua, Blue, Purple | the app's values | the same |

Outbound is an exact lookup: those values come back from Home Assistant exactly
as the favourite stored them. Inbound allows three counts per channel, because
`full_scale_rgbw` scales a dimmed legacy colour back up and rounds (4.3); the
closest pair of palette colours is thirty apart, so the tolerance cannot
mistake one for another. Below about a tenth of full brightness a dimmed colour
has lost what identified it -- a yellow dimmed to 1 is `(1, 0, 0, 0)`, which is
red -- and it is then shown as reported, highlighting no swatch.

Two consequences worth knowing. Picking exactly `255, 255, 255, 0` on the wheel
means Bright White, so all four channels light; red, green and blue alone is
Cool White's `244, 253, 255, 0`. And the translation applies to every command
carrying a palette value, the two actions included, so the same four numbers
mean the same thing everywhere.

### 4.13 Errors

| Cause | Raised as |
|---|---|
| A field fails the schema | `vol.Invalid`, which Home Assistant re-raises as it is |
| Input the schema can't check, like an unknown effect or a pixel beyond the count | `ServiceValidationError` |
| The controller refused the command or couldn't be reached | `HomeAssistantError`, from `_async_send` |

All three are translated in `strings.json`. Tests for bad fields expect
`vol.Invalid`.

### 4.14 The simulator follows the document

`tools/hub2_sim.py` serves the three routes over a real socket and rejects what
the document says the firmware rejects (405, 411, 413, 415 and 505). A simulator
that's more forgiving than the device lets bugs through. It also answers
out-of-range values with a 400, which the document doesn't promise (Q5), and its
`clamp` mode covers the other possibility. A contract test runs everything
`api.py` can build through the simulator's validation, so the two can't drift.

It also covers Bluetooth `B800`/`B801`, power cycles, reboots onto new firmware,
every documented error code, several controllers in one process, and unreliable
links (`latency`, `flaky`, `lossy`, `busy-burst`). Its three reply modes match the
possible answers to open questions: `applied` (the default), `accepted` (Q1) and
`on-state-only` (section 3.4).

### 4.15 Discovery rides on the Control4 protocol

Home Assistant has no SDDP support (SSDP is a different protocol on port 1900),
and no integration in core or HACS uses it. The one library on PyPI,
`sddp-discovery-protocol`, depends on a C extension with no musl wheels, so it
cannot be installed on Home Assistant OS, and its SEARCH carries only a `Host`
header while the firmware's parser ignores any SEARCH without `From`, `Tran`
and `Timeout`. So `sddp.py` is the client, standard library only, sending the
one request shape the firmware answers.

Three routes lead to the same confirm step, and none is required to work on
its own:

1. The listener hears `NOTIFY ALIVE` and `NOTIFY IDENTIFY`. This needs
   nothing from the controller but the ability to send multicast, so it works
   on every controller, on Wi-Fi or Ethernet, within five minutes of boot.
   `IDENTIFY` is what the Gemstone app's Control4 Identify button (Device
   Settings → Advanced Settings) makes the controller send, by way of the
   cloud, so it is the user's way to skip the five-minute wait. `OFFLINE`
   starts nothing.
2. A search at start-up and every fifteen minutes. The request goes to the
   multicast group *and* to every broadcast address, because the firmware's
   Ethernet stack never joins the group and drops multicast, while it answers
   a broadcast. Whether the Wi-Fi module delivers either is unverified.
3. Home Assistant's DHCP watcher, matching the `gemstone-*` hostname
   (manifest.json). Needs the `dhcp` integration, which `default_config`
   loads.

The firmware unicasts its reply to the address in the request's `From` header
at the port the request came from, so each search socket is bound to the one
address it advertises. Home Assistant in Docker bridge mode never sees replies,
as with every other discovery.

The serial arrives in two spellings: the controller's own from SDDP, and
lower-cased from DHCP. `unique_id_for_serial` normalises both, so the two
routes cannot create two entries. The serial is not the entity or device
identity (4.4): an entry made by hand may never learn it.

A discovered controller is confirmed over HTTP. Because discovery has already
shown it to be on the network, a connection failure there means Allow Local
Commands is off, and the error says so instead of "check the address".

### 4.16 Quality scale

`quality_scale.yaml` grades the integration against Home Assistant's
[Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/).
The manifest claims no tier because two Bronze rules are still open: `brands`
needs artwork, and `dependency-transparency` needs the client published on PyPI.
Both wait for the API's public release.

### 4.17 A pattern is shaped by its animation

The controller's 29 animations do not take the same pattern. Thirteen take no
`direction` at all and the key must be absent; the other sixteen accept only a
subset of the six. Fifteen use a `backgroundColor` and fourteen have none.
Twelve read `extraParameters` -- 28 knobs between them, a wave's length, the
spacing between colour segments, an eyeball's pupil colour.

`animations.py` holds that table, transcribed from Gemstone's animation
specification for the customer app, and `build_pattern_command` shapes each
command from it. `const.ANIMATIONS` is derived from the same table, so the names
in the effect picker cannot drift from the rules that build the command behind
them.

This was a real bug, not tidiness. Sending nine fixed keys for all 29 meant the
twelve with parameters ran on whatever those fields happen to hold in firmware,
which is why animations started from Home Assistant looked wrong while the same
pattern saved from the app looked right: a replay copies the controller's own
reported object (4.11), so the app's parameters survived where ours were never
sent.

Defaults only. The 28 parameters are sent at the values the app would send, and
nothing in Home Assistant exposes them: 28 controls across 12 animations is a
great deal of surface for knobs most people will never touch, and anyone who
wants them can build the pattern in the app and save it here.

Music-mode animations are deliberately absent. They need a UDP audio stream this
integration does not implement, and without it they show nothing.

The simulator checks the shape of what it receives -- direction within the six,
speed 1–255, a well-formed `extraParameters` -- but not which indexes belong to
which animation. That stays in the integration's own table test, so the
simulator remains an independent reader of the wire format rather than a second
copy of the catalog (4.14).

## 5. Open questions

The documentation doesn't settle these and hardware hasn't confirmed them. The
code guards each one, so none can send a wrong command.

| # | Question | Answering it would allow | Guarded today by |
|---|---|---|---|
| Q1 | Does a play reply mean the change was applied, or only accepted? | Deciding whether `async_apply_result` needs a follow-up refresh | 5-second polling after every change |
| Q2 | ~~Is `bluetoothName` the serial number?~~ Answered in 0.6: no, it is a user-set name. The serial is the SDDP `Host` header and the DHCP hostname, `Gemstone-<serial>`. | Done: a serial-based unique id and discovery (4.15) | — |
| Q3 | Do pixel numbers start at 0 or 1, count per output or across the controller, and do unlisted pixels go dark? | Zone light entities and a tighter pixel check | Only numbers above the total are rejected |
| Q4 | ~~What do direction values 1–10 do?~~ Answered: there are six, 0 Right, 1 Left, 2 Right & Left, 3 In, 4 Out, 5 In & Out, and each animation accepts only some of them. The local API document's 1–10 is a looser outer bound than anything an animation reads. | Done: the catalog picks the direction per animation (4.17) | — |
| Q5 | Does the firmware reject out-of-range values with a 400, clamp them, or ignore them? | Knowing whether the simulator's 400 is right | The integration never sends one |
| Q6 | Can the local API list or target the app's zones? Alexa and Google reach them through the cloud, but the local API can't. | Zone entities without pixel numbers | Nothing: a zone is a list of pixels |
| Q7 | What do `playlist` and `impulse` look like, and what does `lengthInSeconds` control? | Playing playlists and impulses | Recognised as scenes, never sent |

The simulator also assumes two things nobody has confirmed: an 8 KiB limit
behind the documented 413, and that the scene survives a power cycle.

## 6. Extending it

To add an animation, add it to `const.ANIMATIONS`. The effect list, the bench
buttons and the `services.yaml` options follow, and a test fails until the YAML
is regenerated.

To add a hub-settings sensor, add a description with a `value_fn` (and an
`exists_fn` if older firmware might leave the field out), plus a translation in
both `strings.json` and `translations/en.json`. Custom integrations only read
`translations/`.

To add an action, put the schema in `services.py`, the description in
`services.yaml`, the strings in `strings.json`, an icon in `icons.json`, and the
handler on the light. A test checks that the YAML fields match the schema.

To support a new payload, build it in `api.py`, validate it in
`tools/hub2_sim.py`, and add the shape to `tests/payloads.py`.

Whatever you add, the integration never assumes state it didn't read from the
controller.

## 7. Roadmap

1. Zones. The proper fix is firmware that exposes the app's zones and accepts a
   zone target (Q6). Until then, zones defined in the options flow (a name and a
   pixel range, each a light entity, combined into one architectural design)
   could work for static colours, once Q3 is answered. Nothing per-pixel is
   exposed in the meantime (4.5).
2. Playing playlists and impulses (Q7).
3. ~~Discovery.~~ Done in 0.6 (4.15). Still open on the firmware side: a
   `serial` field in hub-settings would let an entry added by hand learn its
   serial over HTTP, and an announcement right after the UDP server opens
   would make a rebooted controller appear in seconds rather than five
   minutes.
4. Brand artwork, a licence, and `api.py` on PyPI, after the API's public release.
5. mypy and `py.typed`.
