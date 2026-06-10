"""
Connectix Smart Meter Gateway v1.0 device plugin.

REST API endpoint: http://<host>:82/smartmeter/api/read
Returns JSON with power, energy, voltage, current per phase.

config.json keys:
  host          IP address of the gateway
  port          Port (default: 82)
  username      Optional HTTP basic auth username
  password      Optional HTTP basic auth password
  poll_interval Seconds between polls (default: 10)
"""

import asyncio
import json
import urllib.request
import urllib.error
from typing import Optional
from devices.registry import BaseDevice


def _fetch(host: str, port: int, username: str, password: str) -> dict:
    url = f"http://{host}:{port}/smartmeter/api/read"
    req = urllib.request.Request(url, headers={"User-Agent": "HomeControl/1.0"})

    if username and password:
        import base64
        creds = base64.b64encode(f"{username}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach Smart Meter Gateway at {url}: {e}")


class SmartMeterGateway(BaseDevice):
    device_type = "power_meter"

    def __init__(
        self,
        host: str,
        port: int = 82,
        username: str = "",
        password: str = "",
        poll_interval: int = 10,
    ):
        super().__init__()
        self.host          = host
        self.port          = port
        self.username      = username
        self.password      = password
        self.poll_interval = poll_interval

    async def snapshot(self) -> dict:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(
            None, _fetch, self.host, self.port, self.username, self.password
        )

        def f(key, default=None):
            """Parse a float field, return default if zero or missing."""
            try:
                return float(raw.get(key, 0)) or default
            except (ValueError, TypeError):
                return default

        # Net power: positive = consuming, negative = returning to grid
        delivered = f("PowerDelivered_total", 0)
        returned  = f("PowerReturned_total",  0)
        net_kw    = round(delivered - returned, 3)

        return {
            # ── Totals ──────────────────────────────────────────────────
            "net_power_kw":          net_kw,
            "power_delivered_kw":    round(delivered, 3),
            "power_returned_kw":     round(returned, 3),
            "power_netto_kw":        f("PowerDeliveredNetto"),

            # ── Energy counters (kWh) ────────────────────────────────────
            "energy_delivered_t1_kwh": f("EnergyDeliveredTariff1"),
            "energy_delivered_t2_kwh": f("EnergyDeliveredTariff2"),
            "energy_returned_t1_kwh":  f("EnergyReturnedTariff1"),
            "energy_returned_t2_kwh":  f("EnergyReturnedTariff2"),
            "energy_this_hour_kwh":    f("PowerDeliveredHour"),

            # ── Per-phase power (W) ──────────────────────────────────────
            "power_l1_w":  f("PowerDelivered_l1") or -(f("PowerReturned_l1") or 0),
            "power_l2_w":  f("PowerDelivered_l2") or -(f("PowerReturned_l2") or 0),
            "power_l3_w":  f("PowerDelivered_l3") or -(f("PowerReturned_l3") or 0),

            # ── Voltage (V) ──────────────────────────────────────────────
            "voltage_l1_v": f("Voltage_l1"),
            "voltage_l2_v": f("Voltage_l2"),
            "voltage_l3_v": f("Voltage_l3"),

            # ── Current (A) ──────────────────────────────────────────────
            "current_l1_a": f("Current_l1"),
            "current_l2_a": f("Current_l2"),
            "current_l3_a": f("Current_l3"),

            # ── Gas ──────────────────────────────────────────────────────
            "gas_delivered_m3":      f("GasDelivered"),
            "gas_this_hour_m3":      f("GasDeliveredHour"),

            # ── Gateway info ─────────────────────────────────────────────
            "firmware":              raw.get("firmware_running"),
            "firmware_update":       raw.get("firmware_update_available") == "true",
            "wifi_rssi_dbm":         int(raw.get("wifi_rssi", 0) or 0),
            "tariff":                raw.get("ElectricityTariff", ""),
        }
