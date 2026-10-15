"""
Shelly Duo Bulb Gen3 device plugin.

Uses the local RPC API: GET http://<host>/rpc/CCT.Set?id=0&on=true&brightness=50&ct=2700
No authentication required by default.

config.json keys:
  host          IP address of the bulb
  name          Friendly name (e.g. "Living Room", "Outside")
  poll_interval Seconds between status polls (default: 30)

snapshot() returns:
  on            True if bulb is on
  brightness    0-100 %
  ct            Color temperature in Kelvin (2700-6500)
  power_w       Current power draw in watts (if available)

command() supports:
  set     params: {on: bool, brightness: int, ct: int, transition: int (ms)}
  toggle  params: {}
"""

import asyncio
import json
import urllib.request
import urllib.error
from devices.registry import BaseDevice


def _rpc_get(host: str, method: str, params: dict) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"http://{host}/rpc/{method}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "HomeControl/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read().decode()
            return json.loads(data) if data.strip() not in ("null", "") else {}
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach Shelly Bulb at {host}: {e}")


def _fetch_status(host: str) -> dict:
    return _rpc_get(host, "CCT.GetStatus", {"id": 0})


class ShellyBulb(BaseDevice):
    device_type = "light"

    def __init__(self, host: str, name: str = "", poll_interval: int = 30):
        super().__init__()
        self.host          = host
        self.bulb_name     = name or host
        self.poll_interval = poll_interval

    async def snapshot(self) -> dict:
        loop = asyncio.get_event_loop()
        raw  = await loop.run_in_executor(None, _fetch_status, self.host)
        return {
            "bulb_name":  self.bulb_name,
            "on":         raw.get("output", False),
            "brightness": raw.get("brightness", 0),
            "ct":         raw.get("ct", 2700),
            "power_w":    raw.get("apower"),
            "voltage_v":  raw.get("voltage"),
        }

    async def command(self, cmd: str, params: dict) -> dict:
        loop = asyncio.get_event_loop()

        if cmd == "set":
            rpc_params = {"id": 0}
            if "on" in params:
                rpc_params["on"] = str(params["on"]).lower()
            if "brightness" in params:
                rpc_params["brightness"] = int(params["brightness"])
            if "ct" in params:
                rpc_params["ct"] = int(params["ct"])
            if "transition" in params:
                rpc_params["transition_duration"] = float(params["transition"]) / 1000
            await loop.run_in_executor(None, _rpc_get, self.host, "CCT.Set", rpc_params)
            return {"ok": True}

        if cmd == "toggle":
            await loop.run_in_executor(None, _rpc_get, self.host, "CCT.Toggle", {"id": 0})
            return {"ok": True}

        if cmd == "on":
            br = params.get("brightness", 100)
            ct = params.get("ct", 2700)
            tr = params.get("transition", 1000)
            await loop.run_in_executor(None, _rpc_get, self.host, "CCT.Set",
                {"id": 0, "on": "true", "brightness": int(br), "ct": int(ct),
                 "transition_duration": float(tr) / 1000})
            return {"ok": True}

        if cmd == "off":
            tr = params.get("transition", 1000)
            await loop.run_in_executor(None, _rpc_get, self.host, "CCT.Set",
                {"id": 0, "on": "false", "transition_duration": float(tr) / 1000})
            return {"ok": True}

        raise NotImplementedError(f"Unknown command '{cmd}'")
