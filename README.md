# Gemstone Lights for Home Assistant

Home Assistant integration for the Gemstone Lights Hub2 controller. It talks to
the controller over the local network: no cloud account, and it keeps working
when the internet is down.

## Setup

### 1. Requirements

- Gemstone Hub2 controller. Hub1 (the older white unit) isn't supported.
- The Gemstone app
- Home Assistant 2024.12 or newer, on the same Wi-Fi network as the controller

### 2. Enable local control

In the Gemstone app: Settings → Advanced Settings → turn on **Allow Local
Commands**. No IP address or DHCP reservation is needed; Home Assistant discovers
the controller (step 4).

### 3. Install the integration

1. [Download the ZIP](https://github.com/GemstoneLights/home_assistant_gemstone_lights/archive/refs/heads/main.zip)
   and unzip it.
2. Copy `custom_components/gemstone_lights` into `config/custom_components/`,
   creating `custom_components` if needed. This file should then exist:

   ```
   config/custom_components/gemstone_lights/manifest.json
   ```

   <details>
   <summary>Accessing the config folder</summary>

   1. Settings → Apps (Add-ons in older versions): install **Samba share**.
   2. On its Configuration tab, set a username and password, save, then
      **Start**.
   3. Connect to `\\homeassistant.local\config` (Windows File Explorer) or
      `smb://homeassistant.local` (macOS Finder → Go → Connect to Server).

   </details>

3. Restart Home Assistant: Settings → System → power icon (top right) →
   **Restart Home Assistant**.

### 4. Add the controller

Home Assistant discovers the controller three ways:

- The controller's SDDP (Control4) announcement, every 5 minutes.
- A search at Home Assistant start-up and every 15 minutes.
- Its DHCP hostname, `Gemstone-<serial>`.

It appears under Settings → Devices & services → **Discovered** as
*Gemstone-<serial> (IP address)*, usually within 5 minutes. To trigger it
immediately, tap **Control4 Identify** in the Gemstone app (Device Settings →
Advanced Settings).

Tap **Add**, then **Submit**. The device is named after the controller in the
Gemstone app. Add multiple controllers one at a time; each has its own
Discovered card, identified by serial.

If the controller's IP address changes, Home Assistant follows it within a few
minutes.

#### Troubleshooting

- **"Did not answer over HTTP":** turn on Allow Local Commands (step 2) and
  submit again.
- **Not under Discovered:** check that the controller is online (it announces
  itself only after connecting to Gemstone's servers, about 5 minutes after
  power-up) and that Home Assistant is on the same network (in Docker, use host
  networking). Then **Add integration** → Gemstone Lights to search again.
- **Still not found:** enter the controller's IP address, port 80. It's in the
  Gemstone app's device settings, or in your router's device list as
  `Gemstone-<serial>`.

## Test checklist

Takes about 30 minutes. Test after dark so colours are easy to judge.

| Action | Expected |
|---|---|
| Turn the light off and on. | Responds within a few seconds and restores what was showing. |
| Pick several colours, including red. | Lights match; red is red, not blue. |
| Change brightness. | Lights dim and brighten. |
| Open the colour picker. | Favourites show the Gemstone app's colours, including its three whites. |
| Pick animations from the effect list (e.g. Chase, Fireworks). | Each plays as in the Gemstone app. |
| Move Animation speed while an animation plays. | Speed changes. |
| Change the colour in the Gemstone app. | Home Assistant updates within ~30 s. |
| Power off the controller for a minute, then on. | Unavailable, then reconnects within ~2 min. |
| Restart Home Assistant. | Light returns and works. |
| Before adding the controller, tap Control4 Identify. | Appears under Discovered within seconds. |
| Change the controller's IP (DHCP reservation, then power-cycle). | Same device works at the new address within a few minutes. |

## Saved patterns

The controller can't store Gemstone app patterns and Home Assistant can't
rebuild them, so Home Assistant saves them. They survive restarts; nothing is
written to the controller.

- **Save:** play the pattern in the Gemstone app, then run **Gemstone Lights:
  Save the playing pattern** (Developer tools → Actions). The name defaults to
  the app's.
- **Play:** pick it in the controller's **Pattern** select.
- **Delete:** run **Gemstone Lights: Forget a saved pattern** with its name.

### Patterns and effects combine

A pattern is colours plus an animation. The light's **Effect** list holds the
controller's 29 animations and *off*; **Pattern** holds saved patterns.

Picking an effect while a pattern plays runs that animation in the pattern's
colours and speed: *Canada Day* + *Fireworks* plays Fireworks in Canada Day's
colours, and Pattern still shows *Canada Day*. Save the combination with **Save
the playing pattern**. Effect *off* stops the animation and leaves a plain
colour.

## Dashboard card

The default device page sorts entities alphabetically. A tile card puts the
controls in one place:

```yaml
type: tile
entity: light.h2_0324_xxxx
features:
  - type: light-brightness
  - type: light-effect
  - type: light-color-favorites
```

Add a second tile for `select.h2_0324_xxxx_pattern` with a `select-options`
feature for saved patterns. Use your own entity ids (Settings → Devices &
services → Entities).

`light-effect` and `light-color-favorites` need Home Assistant 2026.9 or newer.
On older versions, pick effects from the light's dialog.

## Notes

- The controller is one light entity; zones can't be controlled separately.
- Designs and zones saved in the Gemstone app aren't available. A per-pixel
  design started from the app still shows in Home Assistant and follows the
  brightness slider.
- Colour favourites start as the Gemstone app's swatches; your edits and
  ordering are kept.
- The three whites look the same in the picker (a screen has no warm channel)
  but differ on the lights: warm white uses the warm LEDs, cool white the RGB
  LEDs, bright white all of them.

[Report issues](https://github.com/GemstoneLights/home_assistant_gemstone_lights/issues)
with what you did and what happened.
