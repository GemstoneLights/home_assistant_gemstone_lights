"""Controller response payloads shared by the component tests.

Shapes come from the API documents; keeping them here means a firmware contract
change breaks tests in one visible place.
"""

from typing import Any

HOST = "192.168.1.50"
BASE = f"http://{HOST}:80"
SETTINGS_URL = f"{BASE}/device-state/hub-settings"
PLAYING_URL = f"{BASE}/device-state/currently-playing"
PLAY_URL = f"{BASE}/device-control/play"

HUB_SETTINGS_RESPONSE: dict[str, Any] = {
    "state": {
        "reported": {
            "hubSettings": {
                # Two populated outputs, so per-output handling is exercised.
                "pixelCount": [104, 52, 0, 0],
                "localIp": HOST,
                "bluetoothName": "Device Name",
                "tcpEnabled": True,
                "location": {"name": "Calgary, AB", "lat": 51.05, "long": -114.07},
                "firmware": "1.1.5",
                "firmwareSpi": "1.2.1",
                "firmwareWifi": "3.3.9",
            },
            "origin": "http",
            "env": "prod",
        }
    }
}

COLOR_ON_RESPONSE: dict[str, Any] = {
    "state": {
        "reported": {
            "currentlyPlaying": {
                "color": None,
                "pattern": None,
                "architectural": None,
                "onState": True,
                "colorB": {"value": 9999999, "brightness": 16},
            },
            "origin": "http",
            "env": "prod",
        }
    }
}

COLOR_OFF_RESPONSE: dict[str, Any] = {
    "state": {
        "reported": {
            "currentlyPlaying": {
                "colorB": {"value": 9999999, "brightness": 16},
                "onState": False,
            },
            "env": "prod",
        }
    }
}

PATTERN_RESPONSE: dict[str, Any] = {
    "state": {
        "reported": {
            "currentlyPlaying": {
                "color": None,
                "colorB": None,
                "architectural": None,
                "onState": True,
                "pattern": {
                    "name": "IsoFade",
                    "animation": "isofade",
                    "id": "8d8b1e70-4ed4-439e-b096-52446653d758",
                    "referencePatternId": "93a145b7-e526-4a3c-b315-b1dbb6193eab",
                    "backgroundColor": 0,
                    "brightness": 255,
                    "speed": 255,
                    "colors": [255, 65280, 16711680],
                },
            },
            "env": "prod",
        }
    }
}

# Section 3.3 of the Control4 document. Indices stay below 104 so they fit the
# first output of HUB_SETTINGS_RESPONSE under every reading of the index space.
ARCHITECTURAL_RESPONSE: dict[str, Any] = {
    "state": {
        "reported": {
            "currentlyPlaying": {
                "onState": True,
                "architectural": {
                    "name": "Front Roofline Design",
                    "id": "3d3f33d3-638f-43b4-ac5f-3efdde6bd0b9",
                    "preview": False,
                    "brightness": 255,
                    "staticColors": [
                        {"color": 12614435, "lights": [4, 8, 9, 11, 19, 29, 30, 36, 38, 39]},
                        {"color": 26367, "lights": [6, 13]},
                        {"color": 110049, "lights": [16, 17, 24, 25]},
                    ],
                },
            },
            "env": "prod",
        }
    }
}

ON_ONLY_RESPONSE: dict[str, Any] = {"state": {"reported": {"currentlyPlaying": {"onState": True}, "env": "prod"}}}

OFF_ONLY_RESPONSE: dict[str, Any] = {"state": {"reported": {"currentlyPlaying": {"onState": False}, "env": "prod"}}}
