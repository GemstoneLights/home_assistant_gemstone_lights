# Gemstone Lights for Home Assistant

Control a Gemstone Lights Hub2 controller from Home Assistant. It talks to the
controller directly over your home network, so there's no cloud account, and it
keeps working when the internet is down.

## Test your Gemstone lights in Home Assistant

This guide gets your Hub2 controller working in Home Assistant, then gives you a
few things to try. It takes about 30 minutes. Do it after dark if you can, so you
can see the lights.

### 1. What you need

- A Gemstone Hub2 controller. The older white Hub1 won't work.
- The Gemstone app on your phone
- Home Assistant 2024.12 or newer, on the same Wi-Fi network as the controller
- A computer

### 2. Set up the controller

1. In the Gemstone app, open your controller and go to Settings → Advanced
   Settings. Turn on **Allow Local Commands**.
2. You should not need the controller's IP address: Home Assistant finds the
   controller by itself (step 4). If you ever do, it looks like `192.168.1.50`
   and is shown in the Gemstone app's device settings, or in your router's list
   of devices.

### 3. Install the integration

1. [Download the ZIP](https://github.com/GemstoneLights/home_assistant_gemstone_lights/archive/refs/heads/main.zip)
   and unzip it. You can also use Code → Download ZIP at the top of this page.
2. In the unzipped files, find the `gemstone_lights` folder (it's inside
   `custom_components`). Copy it into the `custom_components` folder of your Home
   Assistant config folder. If `custom_components` doesn't exist, create it.

   When you're done, this file should exist:

   ```
   config/custom_components/gemstone_lights/manifest.json
   ```

   <details>
   <summary>Not sure how to open the config folder?</summary>

   1. In Home Assistant, go to Settings → Apps (called Add-ons in older
      versions). Find **Samba share** in the store and install it.
   2. On its Configuration tab, set a username and password and save. Then press
      **Start**.
   3. On Windows, type `\\homeassistant.local\config` in the File Explorer address
      bar. On a Mac, open Finder, choose Go → Connect to Server and enter
      `smb://homeassistant.local`. Sign in with the username and password you set.

   </details>

3. Restart Home Assistant: Settings → System, tap the power icon in the top
   right, then **Restart Home Assistant**.

### 4. Connect the controller

Home Assistant finds the controller by itself. The controller announces itself
on the network every five minutes, so within five minutes of restarting Home
Assistant (or of the controller powering up) it appears in Settings → Devices &
services under **Discovered**, as *Gemstone-<serial> (IP address)*.

To skip the wait, make the controller announce itself now: in the Gemstone app,
open the controller and go to Device Settings → Advanced Settings, then tap
**Control4 Identify**. The controller appears under Discovered within a few
seconds.

1. Tap **Add** on the discovered controller, then **Submit**.
2. A new device appears, named after your controller. Tap it to see the light and
   its controls.

If you have more than one controller, each gets its own Discovered card, telling
them apart by serial number; add them one at a time. Once added, each is named
after its name in the Gemstone app.

If it says the controller did not answer over HTTP, turn on Allow Local Commands
(step 2) and submit again.

If it doesn't show up under Discovered, tap **Add integration** and choose
Gemstone Lights. It searches the network once more and offers what it finds; if
yours still isn't listed, enter its IP address and leave the port at 80. Home
Assistant needs to be on the same network as the controller for discovery to
work; in Docker, that means host networking.

### 5. Try it out

Open your controller's device page and go through these.

| Try this | You should see |
|---|---|
| Turn the light off, then on again. | It responds within a few seconds and comes back to what it was showing. |
| Pick a few colours, including red. | The lights match. Red should look red, not blue. |
| Change the brightness. | The lights get dimmer and brighter. |
| Open the colour picker. | The favourites row offers the Gemstone app's colours, including its three whites. |
| Pick a few animations, like Chase or Fireworks, from the light's list. | Each one plays the way it does in the Gemstone app. |
| While an animation is playing, move the Animation speed slider. | It speeds up and slows down. |
| Change the colour in the Gemstone app. | Home Assistant shows the new colour within about 30 seconds. |
| Switch the controller off at the power for a minute, then back on. | It shows as Unavailable, then reconnects by itself within about 2 minutes. |
| Restart Home Assistant. | The light comes back and still works. |
| With the controller not yet added, tap Control4 Identify in the Gemstone app (Device Settings → Advanced Settings). | The controller appears under Discovered within a few seconds. |
| Give the controller a different IP address (change its DHCP reservation and power-cycle it). | Within a few minutes the same device works at the new address, with no re-adding. |

### Save a pattern you like

Patterns you build in the Gemstone app can't be rebuilt by hand in Home
Assistant, and the controller has nowhere to keep them. Home Assistant can.

1. Play the pattern from the Gemstone app.
2. In Home Assistant, open Developer tools → Actions, choose **Gemstone Lights:
   Save the playing pattern**, pick your controller and tap **Perform action**.
   Give it a name, or let it keep the one from the app.
3. It joins the **Pattern** control on the controller's device page. Pick it
   there any time to play that pattern again.

To remove one, run **Gemstone Lights: Forget a saved pattern** and give its name.
Saved patterns live in Home Assistant, so they survive restarts, and nothing is
written to the controller.

### Mix a pattern's colours with a different animation

A pattern is a set of colours *and* an animation, so the two combine rather than
replace each other:

| Where | What it holds |
|---|---|
| The light's **Effect** list | The controller's 29 animations, plus *off* |
| The **Pattern** control | The patterns you have saved |

1. In **Pattern**, pick your saved pattern, say *Canada Day*.
2. On the light, pick an effect, say *Fireworks*.

The lights now run Fireworks in Canada Day's colours, at its speed, and the two
say so together -- Pattern still reads *Canada Day*, because those are still its
colours. To keep the combination, run **Gemstone Lights: Save the playing
pattern** and name it; it becomes its own entry in Pattern, one tap from then on.

Picking **off** in the effect list stops the animation and leaves the lights on a
plain colour.

### Build a dashboard card

The default dashboard sorts a device's rows alphabetically, which rarely puts
them where you want. Build your own card instead and everything is one tap, with
no dialogs:

```yaml
type: tile
entity: light.h2_0324_xxxx
features:
  - type: light-brightness
  - type: light-effect
  - type: light-color-favorites
```

Add a second tile for `select.h2_0324_xxxx_pattern` with a `select-options`
feature and your saved patterns are a tap away too. Use your own entity ids,
which you can copy from Settings → Devices & services → Entities.

The **Light effect** and **Light color favorites** features need Home Assistant
2026.9 or newer. On older versions the effect list still works, but from the
light's dialog rather than the card.

### Good to know

- Designs and zones saved in the Gemstone app don't show up in Home Assistant.
- The whole controller is one light, so you can't control zones separately.
- Colours, animations and saved patterns are what Home Assistant controls.
  Per-pixel designs stay in the Gemstone app; one playing there still shows
  up here and dims with the brightness slider.
- The effect list and **Pattern** work together, not instead of each other, and
  between them they show what is playing now.
- The colour picker starts with the Gemstone app's swatches. Edit or reorder
  them in the favourites row and your version is kept.
- The three whites look identical in the picker, because a screen has no warm
  channel, but they differ on the lights: warm white uses the warm LEDs, cool
  white the red, green and blue ones, and bright white all of them.

If something doesn't work the way this guide says, note what you did and what
happened, and [let us know](https://github.com/GemstoneLights/home_assistant_gemstone_lights/issues).
