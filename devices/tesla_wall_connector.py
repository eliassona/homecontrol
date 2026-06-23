"""
Tesla Wall Connector Gen 3 device plugin.

Local REST API — no authentication required.
Endpoint: http://<host>/api/1/vitals

config.json keys:
  host          IP address of the Wall Connector
  poll_interval Seconds between polls (default: 10)

EVSE state codes:
  0 = Booting
  1 = Ready (no vehicle)
  2 = Connected (vehicle plugged in, not charging)
  3 = Charging
  4 = Error
  5 = Scheduled (charge delayed)
  6 = Busy
  7 = Disconnecting
"""

import asyncio
import json
import urllib.request
import urllib.error
from devices.registry import BaseDevice


EVSE_STATES = {
    0: "Booting",
    1: "Ready",
    2: "Connected",
    3: "Charging",
    4: "Error",
    5: "Scheduled",
    6: "Busy",
    7: "Disconnecting",
}


def _fetch(host: str) -> dict:
    url = f"http://{host}/api/1/vitals"
    req = urllib.request.Request(url, headers={"User-Agent": "HomeControl/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach Tesla Wall Connector at {url}: {e}")


class TeslaWallConnector(BaseDevice):
    device_type = "ev_charger"

    def __init__(self, host: str, poll_interval: int = 10):
        super().__init__()
        self.host          = host
        self.poll_interval = poll_interval

    async def snapshot(self) -> dict:
        loop = asyncio.get_event_loop()
        raw  = await loop.run_in_executor(None, _fetch, self.host)

        def f(key):
            try:
                return raw[key]
            except KeyError:
                return None

        evse_state = f("evse_state") or 0
        charging   = evse_state == 3

        # Power in watts: sum phase currents × grid voltage
        # When charging, currentA/B/C will be non-zero
        i_a = f("currentA_a") or 0
        i_b = f("currentB_a") or 0
        i_c = f("currentC_a") or 0
        grid_v = f("grid_v") or 0
        # Single-phase: only one phase active; three-phase: all three
        total_current = i_a + i_b + i_c
        power_w = round(total_current * grid_v, 0) if charging else 0

        # Session duration
        session_s = f("session_s") or 0
        h = session_s // 3600
        m = (session_s % 3600) // 60
        session_str = f"{h}h {m}m" if h else (f"{m}m" if m else "—")

        return {
            # Status
            "evse_state":        evse_state,
            "evse_state_str":    EVSE_STATES.get(evse_state, f"Unknown ({evse_state})"),
            "vehicle_connected": f("vehicle_connected") or False,
            "contactor_closed":  f("contactor_closed") or False,
            "charging":          charging,
            "current_alerts":    f("current_alerts") or [],

            # Power
            "power_w":           power_w,
            "vehicle_current_a": f("vehicle_current_a") or 0,
            "current_a_a":       i_a,
            "current_b_a":       i_b,
            "current_c_a":       i_c,
            "grid_v":            grid_v,
            "grid_hz":           f("grid_hz"),

            # Session
            "session_energy_wh": f("session_energy_wh") or 0,
            "session_s":         session_s,
            "session_str":       session_str,

            # Temperatures
            "pcba_temp_c":       f("pcba_temp_c"),
            "handle_temp_c":     f("handle_temp_c"),
            "mcu_temp_c":        f("mcu_temp_c"),

            # Uptime
            "uptime_s":          f("uptime_s"),
        }
