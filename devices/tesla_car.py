"""
Tesla Model Y (and other Tesla vehicles) device plugin.

Uses the Tesla Fleet API for reading vehicle state and controlling charging.
Requires OAuth authentication via the Tesla developer portal.

SETUP (one-time):
  1. Create a Tesla Developer account at https://developer.tesla.com
  2. Create an application and get client_id + client_secret
  3. Run the auth script to get a refresh_token:
       python3 tesla_auth.py
     (generates token_file on disk — no credentials stored in config after that)
  4. Add to config.json (see keys below)

config.json keys:
  vin              Vehicle Identification Number (on dashboard or Tesla app)
  token_file       Path to token file from auth script (e.g. "tesla_token.json")
  region           Fleet API region: "eu" for Europe (default), "na" for North America
  poll_interval    Seconds between polls (default: 60 — don't poll too fast, uses cellular)

NOTE: Tesla Fleet API requires the car to be awake. If asleep, commands will fail
      with a timeout. The plugin will attempt to wake the car before sending commands.

snapshot() returns:
  battery_pct          State of charge (%)
  battery_range_km     Estimated range
  charge_state         Charging / Stopped / Disconnected / NoPower / Complete
  charging             True if actively charging
  charge_rate_kw       Current charge power in kW
  charge_limit_pct     Configured charge limit (%)
  minutes_to_full      Minutes until charge limit reached
  plugged_in           True if charge cable connected
  locked               Door lock state
  climate_on           True if climate is active
  inside_temp_c        Cabin temperature
  outside_temp_c       Outside temperature from car sensors
  odometer_km          Odometer reading
  software_version     Car firmware version
  vehicle_name         Name set in Tesla app
"""

import asyncio
import json
import logging
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Optional
from devices.registry import BaseDevice

log = logging.getLogger("tesla_car")

FLEET_API_BASE = {
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
}


class TeslaTokenManager:
    """Handles OAuth token refresh for Tesla Fleet API."""

    def __init__(self, token_file: str):
        self.token_file = Path(token_file)
        self._access_token:  Optional[str] = None
        self._expires_at:    float = 0

    def _load(self) -> dict:
        try:
            with open(self.token_file) as f:
                return json.load(f)
        except Exception as e:
            raise ConnectionError(
                f"Cannot read Tesla token file '{self.token_file}': {e}\n"
                "Run tesla_auth.py to generate it."
            )

    def _save(self, data: dict):
        with open(self.token_file, "w") as f:
            json.dump(data, f, indent=2)

    def _refresh(self, data: dict) -> str:
        """Exchange refresh_token for a new access_token."""
        payload = urllib.parse.urlencode({
            "grant_type":    "refresh_token",
            "client_id":     data["client_id"],
            "refresh_token": data["refresh_token"],
        }).encode()
        req = urllib.request.Request(
            "https://auth.tesla.com/oauth2/v3/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            token_data = json.loads(resp.read().decode())

        data["access_token"]  = token_data["access_token"]
        data["refresh_token"] = token_data.get("refresh_token", data["refresh_token"])
        data["expires_at"]    = time.time() + token_data.get("expires_in", 3600) - 60
        self._save(data)
        return data["access_token"]

    def get_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token

        data = self._load()
        # Use cached token from file if still valid
        if time.time() < data.get("expires_at", 0):
            self._access_token = data["access_token"]
            self._expires_at   = data["expires_at"]
            return self._access_token

        # Refresh
        self._access_token = self._refresh(data)
        self._expires_at   = data["expires_at"]
        return self._access_token


def _api_get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "HomeControl/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _api_post(url: str, token: str, body: dict = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization":  f"Bearer {token}",
            "Content-Type":   "application/json",
            "User-Agent":     "HomeControl/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


class TeslaCar(BaseDevice):
    device_type = "tesla_car"

    def __init__(
        self,
        vin: str,
        token_file: str,
        region: str = "eu",
        poll_interval: int = 60,
    ):
        super().__init__()
        self.vin           = vin
        self.region        = region
        self.poll_interval = poll_interval
        self._tokens       = TeslaTokenManager(token_file)
        self._base         = FLEET_API_BASE.get(region, FLEET_API_BASE["eu"])

    def _url(self, path: str) -> str:
        return f"{self._base}/api/1/vehicles/{self.vin}{path}"

    def _fetch_data(self) -> dict:
        token = self._tokens.get_token()
        return _api_get(self._url("/vehicle_data"), token)

    def _send_command(self, command: str, body: dict = None) -> dict:
        token = self._tokens.get_token()
        return _api_post(self._url(f"/command/{command}"), token, body)

    def _wake(self) -> dict:
        token = self._tokens.get_token()
        return _api_post(self._url("/wake_up"), token)

    async def snapshot(self) -> dict:
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(None, self._fetch_data)
        except urllib.error.HTTPError as e:
            if e.code == 408:
                raise ConnectionError("Tesla is asleep — will retry next poll")
            raise

        v = resp.get("response", {})
        charge = v.get("charge_state", {})
        climate = v.get("climate_state", {})
        vehicle = v.get("vehicle_state", {})
        drive   = v.get("drive_state", {})

        charge_state = charge.get("charging_state", "Unknown")
        rate_a       = charge.get("charge_current_request", 0) or 0
        volts        = charge.get("charger_voltage", 0) or 0
        phases       = charge.get("charger_phases", 1) or 1
        charge_kw    = round(rate_a * volts * phases / 1000, 2)

        return {
            "vehicle_name":      vehicle.get("vehicle_name", "Tesla"),
            "battery_pct":       charge.get("battery_level"),
            "battery_range_km":  round((charge.get("battery_range", 0) or 0) * 1.60934, 1),
            "charge_state":      charge_state,
            "charging":          charge_state == "Charging",
            "charge_rate_kw":    charge_kw,
            "charge_limit_pct":  charge.get("charge_limit_soc"),
            "minutes_to_full":   charge.get("time_to_full_charge", 0),
            "plugged_in":        charge.get("charging_state") != "Disconnected",
            "charge_port_open":  charge.get("charge_port_door_open", False),
            "inside_temp_c":     climate.get("inside_temp"),
            "outside_temp_c":    climate.get("outside_temp"),
            "climate_on":        climate.get("is_climate_on", False),
            "locked":            vehicle.get("locked", True),
            "odometer_km":       round((vehicle.get("odometer", 0) or 0) * 1.60934, 0),
            "software_version":  vehicle.get("car_version", ""),
            "vin":               self.vin,
        }

    async def command(self, cmd: str, params: dict) -> dict:
        loop = asyncio.get_event_loop()

        if cmd == "start_charging":
            resp = await loop.run_in_executor(None, self._send_command, "charge_start")
            return {"ok": resp.get("response", {}).get("result", False)}

        if cmd == "stop_charging":
            resp = await loop.run_in_executor(None, self._send_command, "charge_stop")
            return {"ok": resp.get("response", {}).get("result", False)}

        if cmd == "set_charge_limit":
            pct = int(params.get("percent", 80))
            resp = await loop.run_in_executor(
                None, self._send_command, "set_charge_limit", {"percent": pct}
            )
            return {"ok": resp.get("response", {}).get("result", False), "limit": pct}

        if cmd == "wake":
            resp = await loop.run_in_executor(None, self._wake)
            return {"state": resp.get("response", {}).get("state")}

        raise NotImplementedError(f"Unknown command '{cmd}'")
