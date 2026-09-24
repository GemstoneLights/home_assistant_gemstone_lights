#!/usr/bin/env bash
# Launch a throwaway Home Assistant instance that loads this repo's integration.
#
# Idempotent: run it as often as you like. State lives in ha_config/ (gitignored)
# so you onboard once and keep your login between runs.
#
#   tools/dev_ha.sh          then open http://localhost:8123
#
# Pair it with tools/hub2_sim.py, which defaults to port 8080 and needs no
# privileges; add the controller in Home Assistant with host 127.0.0.1, port 8080.
# To see it discovered instead, start the simulator with --host <this machine's
# LAN address>: a discovered controller is confirmed at the address its SDDP
# reply came from, and 127.0.0.1 is never that address.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$REPO/ha_config"
VENV="$REPO/.venv"

if [[ ! -x "$VENV/bin/hass" ]]; then
  echo "error: $VENV/bin/hass not found. Create the venv first (see README)." >&2
  exit 1
fi

# Home Assistant runs with --skip-pip below, so it can never rewrite the venv the
# tests share. The cost is that anything it would have installed has to be here
# already. `hass_frontend` and `isal` are needed to boot; the rest are the
# default integrations onboarding sets up when you finish the core-config step.
# Their absence is not graceful -- onboarding gathers those flows without
# return_exceptions, so one missing module 500s the step and you cannot finish
# onboarding at all.
#
# pycares is the odd one out: nothing is missing, it is too new. Home Assistant
# pins aiodns==3.2.0 but not the pycares under it, and pycares 5 changed the call
# aiodns makes. Every hostname lookup through the shared client session then
# raises TypeError: radio_browser fails to set up, and so does adding a
# controller by name instead of IP (IP literals never reach the resolver).
#
# The last group is for the Home Assistant desktop/mobile app, which registers
# itself through mobile_app straight after login -- without it the app logs in
# and then shows a blank window. mobile_app needs PyNaCl, and imports cloud at
# module level, which drags in assist_pipeline (pymicro-vad, pyspeex-noise) and
# conversation (hassil, home-assistant-intents).
REQUIREMENTS=(
  "home-assistant-frontend==20250109.2"
  "isal"
  "gTTS==2.2.4"      # google_translate
  "ha-ffmpeg==3.2.2" # google_translate imports tts, which imports ffmpeg
  "PyMetno==0.13.0"  # met
  "radios==0.3.2"    # radio_browser
  "pycountry==24.6.1"
  "mutagen"
  "pycares<5"        # see above
  "PyNaCl==1.5.0"
  "hassil==2.1.0"
  "home-assistant-intents==2025.1.1"
  "pymicro-vad==1.0.1"
  "pyspeex-noise==1.0.2"
)
if ! "$VENV/bin/python" - <<'PY' 2>/dev/null
import importlib.util as u, sys
from importlib.metadata import version
missing = [m for m in ("hass_frontend", "isal", "gtts", "haffmpeg", "metno", "radios", "pycountry", "mutagen",
                       "nacl", "hassil", "home_assistant_intents", "pymicro_vad", "pyspeex_noise")
           if u.find_spec(m) is None]
sys.exit(1 if missing or int(version("pycares").split(".")[0]) >= 5 else 0)
PY
then
  echo "installing the packages Home Assistant needs (one time)..."
  "$VENV/bin/uv" pip install --python "$VENV" "${REQUIREMENTS[@]}" \
    || "$VENV/bin/pip" install "${REQUIREMENTS[@]}"
fi

# Home Assistant hard-exits if a -c directory does not exist; it only ever
# auto-creates the default ~/.homeassistant.
mkdir -p "$CONFIG/custom_components"

# Symlink the inner package, not the whole custom_components directory, so a
# second integration can be dropped in later without touching the repo. The
# target must be absolute: a dangling symlink fails Path.is_dir(), and Home
# Assistant then skips the integration *silently* -- "Gemstone" simply returns
# nothing in Add Integration, with nothing in the log to explain why.
ln -sfn "$REPO/custom_components/gemstone_lights" "$CONFIG/custom_components/gemstone_lights"

# Write the config only if absent, so local edits survive. This must exist
# before the first boot: Home Assistant otherwise writes its own template, and
# that template starts with `default_config:` -- see below for why that matters.
if [[ ! -f "$CONFIG/configuration.yaml" ]]; then
  cat > "$CONFIG/configuration.yaml" <<'YAML'
# Deliberately no `default_config:`.
#
# Home Assistant installs integration requirements into the *active venv* when
# it detects one (homeassistant/requirements.py only uses <config>/deps
# outside a venv). This venv is the one the test suite runs in, and
# default_config: would pull the bluetooth/zeroconf/stream stack -- downgrading
# five packages the tests already depend on. bootstrap.py's DEFAULT_INTEGRATIONS
# already gives us frontend, onboarding, person and logger with no config at
# all, which is everything this harness needs.
#
# Discovery: the integration's own SDDP search and listener need nothing extra
# (`network` is a dependency and loads by itself). The DHCP-hostname route is
# the one thing default_config would add; to try it here, add `dhcp:` below and
# aiodhcpwatcher, aiodiscover and cached-ipaddress to REQUIREMENTS in dev_ha.sh.

homeassistant:
  name: Gemstone Dev
  latitude: 51.0447          # matches the sample payloads in docs/
  longitude: -114.0719
  time_zone: America/Edmonton
  unit_system: metric

http:
  # Loopback only: binding 0.0.0.0 trips the macOS firewall prompt on every
  # venv rebuild, and this instance has no business being on the LAN.
  #
  # Both families, because `localhost` resolves to ::1 before 127.0.0.1 and
  # browsers try it first. Listening on IPv4 alone makes http://localhost:8123
  # fail in Chrome while curl still works -- curl falls back to IPv4.
  server_host:
    - 127.0.0.1
    - "::1"

logger:
  default: warning
  logs:
    # Without this the 503 retry-once path is invisible: api.py logs "busy,
    # retrying" at DEBUG and then transparently succeeds, so a retried poll
    # looks identical to a normal one.
    custom_components.gemstone_lights: debug
    # PyTurboJPEG and its native library are not installed, so the camera
    # component logs a traceback on every boot and falls back to slower
    # snapshot resizing. Nothing in this harness takes camera snapshots.
    homeassistant.components.camera.img_util: critical

# The Home Assistant desktop/mobile app registers itself through mobile_app right
# after login. Without it the app authenticates and then shows a blank window.
mobile_app:
YAML
  echo "wrote $CONFIG/configuration.yaml"
fi

# Rebuild the test-bench dashboard every launch, so its effect buttons and its
# entity id follow the code and the registry rather than drifting from them.
"$VENV/bin/python" "$REPO/tools/make_bench_dashboard.py"

echo
echo "config:     $CONFIG"
echo "component:  $(readlink "$CONFIG/custom_components/gemstone_lights")"
echo "open:       http://localhost:8123"
echo "bench:      http://localhost:8123/gemstone-bench/bench"
echo

# --skip-pip keeps Home Assistant from ever writing to the test venv. Nothing is
# lost: manifest.json declares "requirements": [].
exec "$VENV/bin/hass" -c "$CONFIG" --skip-pip
