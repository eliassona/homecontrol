"""
Connectix / SmartGateways Smart Meter Gateway device plugin.

REST API endpoint: http://<host>:<port>/smartmeter/api/read
No authentication required for the API endpoint.

Field notes from real device response:
  PowerDelivered_total / _l1/_l2/_l3  — watts (integer strings)
  PowerReturned_total  / _l1/_l2/_l3  — watts (integer strings)
  EnergyDeliveredTariff1/2            — kWh (float strings)
  Voltage_l1/l2/l3                    — volts (float strings)
  Current_l1/l2/l3                    — amps (zero-padded float strings e.g. "001.5")
  PowerDeliveredHour                  — kWh consumed this hour

config.json keys:
  host          IP address of the gateway
  port          Port (default: 82)
  username      Unused — API has no auth (kept for future use)
  password      Unused — API has no auth (kept for future use)
  poll_interval Seconds between polls (default: 10)
"""

import asyncio
import json
import urllib.request
import urllib.error
from devices.registry import BaseDevice


def _fetch(host: str, port: int) -> dict:
    url = f"http://{host}:{port}/smartmeter/api/read"
    req = urllib.request.Request(url, headers={"User-Agent": "HomeControl/1.0"})
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
        self.username      = username   # reserved
        self.password      = password   # reserved
        self.poll_interval = poll_interval

    async def snapshot(self) -> dict:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, _fetch, self.host, self.port)

        def f(key, scale=1.0):
            """Parse numeric field, apply scale, return None if missing/zero."""
            try:
                return round(float(raw[key]) * scale, 3)
            except (KeyError, ValueError, TypeError):
                return None

        def w(key):
            """Parse a watt field (integer string)."""
            try:
                return int(float(raw[key]))
            except (KeyError, ValueError, TypeError):
                return None

        delivered_w = w("PowerDelivered_total") or 0
        returned_w  = w("PowerReturned_total")  or 0
        net_w       = delivered_w - returned_w

        return {
            # ── Net power ────────────────────────────────────────────────
            "net_power_w":           net_w,
            "net_power_kw":          round(net_w / 1000, 3),
            "power_delivered_w":     delivered_w,
            "power_returned_w":      returned_w,

            # ── Per-phase power (W) ──────────────────────────────────────
            "power_l1_w":            w("PowerDelivered_l1"),
            "power_l2_w":            w("PowerDelivered_l2"),
            "power_l3_w":            w("PowerDelivered_l3"),
            "power_returned_l1_w":   w("PowerReturned_l1"),
            "power_returned_l2_w":   w("PowerReturned_l2"),
            "power_returned_l3_w":   w("PowerReturned_l3"),

            # ── Voltage (V) ──────────────────────────────────────────────
            "voltage_l1_v":          f("Voltage_l1"),
            "voltage_l2_v":          f("Voltage_l2"),
            "voltage_l3_v":          f("Voltage_l3"),

            # ── Current (A) ──────────────────────────────────────────────
            "current_l1_a":          f("Current_l1"),
            "current_l2_a":          f("Current_l2"),
            "current_l3_a":          f("Current_l3"),

            # ── Energy counters (kWh) ────────────────────────────────────
            "energy_delivered_t1_kwh":  f("EnergyDeliveredTariff1"),
            "energy_delivered_t2_kwh":  f("EnergyDeliveredTariff2"),
            "energy_returned_t1_kwh":   f("EnergyReturnedTariff1"),
            "energy_returned_t2_kwh":   f("EnergyReturnedTariff2"),
            "energy_this_hour_kwh":     f("PowerDeliveredHour"),

            # ── Reactive power (kVAr) ────────────────────────────────────
            "reactive_delivered_kvar":  f("ReactivePowerDelivered"),
            "reactive_returned_kvar":   f("ReactivePowerReturned"),

            # ── Gas ──────────────────────────────────────────────────────
            "gas_delivered_m3":         f("GasDelivered"),
            "gas_this_hour_m3":         f("GasDeliveredHour"),

            # ── Gateway info ─────────────────────────────────────────────
            "firmware":                 raw.get("firmware_running"),
            "firmware_update":          raw.get("firmware_update_available") == "true",
            "wifi_rssi_dbm":            int(raw.get("wifi_rssi", 0) or 0),
        }
