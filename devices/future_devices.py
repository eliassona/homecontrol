"""
Future device stubs — uncomment and flesh out as hardware arrives.

Each stub shows the expected snapshot() shape so the dashboard
can be built against a stable schema before real hardware is wired up.
"""

import asyncio
import random
from devices.registry import BaseDevice


# ─── Room Temperature Sensor ────────────────────────────────────────────────
# Hardware: DS18B20 (1-Wire), DHT22, or SHT31 on GPIO / USB
# Library:  w1thermsensor  OR  adafruit-circuitpython-dht

class RoomSensor(BaseDevice):
    device_type = "room_sensor"
    poll_interval = 30

    def __init__(self, sensor_id: str, room_name: str = ""):
        super().__init__()
        self.sensor_id = sensor_id
        self.room_name = room_name or sensor_id

    async def snapshot(self) -> dict:
        # TODO: replace with real hardware read
        # from w1thermsensor import W1ThermSensor
        # sensor = W1ThermSensor()
        # temp = sensor.get_temperature()
        return {
            "room": self.room_name,
            "temperature_c": round(20 + random.uniform(-3, 3), 1),
            "humidity_pct":  round(45 + random.uniform(-10, 10), 1),
        }


# ─── Whole-house Power Meter ─────────────────────────────────────────────────
# Hardware: Shelly EM, Eastron SDM120, or CT clamp on Modbus/HTTP
# Library:  requests (Shelly HTTP) or minimalmodbus (SDM120)

class PowerMeter(BaseDevice):
    device_type = "power_meter"
    poll_interval = 5

    def __init__(self, host: str = "", port: str = ""):
        super().__init__()
        self.host = host
        self.port = port

    async def snapshot(self) -> dict:
        # TODO: Shelly EM example:
        # import requests
        # r = requests.get(f"http://{self.host}/emeter/0", timeout=3)
        # d = r.json()
        # return {"watts": d["power"], "kwh_total": d["total"]}
        return {
            "watts":         round(2400 + random.uniform(-200, 200), 1),
            "kwh_today":     round(18.4 + random.uniform(0, 2), 2),
            "voltage":       round(230 + random.uniform(-5, 5), 1),
            "current_a":     round(10.4 + random.uniform(-1, 1), 2),
        }


# ─── Tesla Wall Connector / Fleet API charger ────────────────────────────────
# Auth: Tesla Fleet API OAuth — see https://developer.tesla.com/

class TeslaCharger(BaseDevice):
    device_type = "ev_charger"
    poll_interval = 30

    def __init__(self, vin: str, access_token: str = ""):
        super().__init__()
        self.vin = vin
        self.access_token = access_token

    async def snapshot(self) -> dict:
        # TODO: Tesla Fleet API
        # GET https://fleet-api.prd.eu.vn.cloud.tesla.com/api/1/vehicles/{vin}/vehicle_data
        return {
            "charging_state":   "Charging",        # Charging | Stopped | Disconnected
            "battery_pct":      72,
            "charge_rate_kw":   7.4,
            "minutes_to_full":  145,
            "charge_limit_pct": 90,
        }

    async def command(self, cmd: str, params: dict) -> dict:
        # TODO: POST /api/1/vehicles/{vin}/command/{cmd}
        if cmd == "set_charge_limit":
            limit = params.get("percent", 80)
            return {"queued": f"set_charge_limit to {limit}%"}
        if cmd == "start_charging":
            return {"queued": "charge_start"}
        if cmd == "stop_charging":
            return {"queued": "charge_stop"}
        raise NotImplementedError(f"Unknown command '{cmd}'")


# ─── WiFi Radiator (Mill / Adax / Sensibo compatible) ────────────────────────
# Most brands expose a local LAN API or cloud REST API

class WifiRadiator(BaseDevice):
    device_type = "radiator"
    poll_interval = 60

    def __init__(self, ip: str, room: str = ""):
        super().__init__()
        self.ip = ip
        self.room = room

    async def snapshot(self) -> dict:
        # TODO: Mill local API example:
        # import requests
        # r = requests.get(f"http://{self.ip}/panel/status", timeout=3)
        return {
            "room":           self.room,
            "set_temp_c":     20,
            "current_temp_c": 18.5,
            "heating":        True,
            "power_w":        1000,
        }

    async def command(self, cmd: str, params: dict) -> dict:
        if cmd == "set_temperature":
            temp = params.get("temp_c", 20)
            # TODO: POST to device
            return {"queued": f"set_temp {temp}°C"}
        raise NotImplementedError(f"Unknown command '{cmd}'")


# ─── Air Heat Pump ─────────────────────────────────────────────────────────
# Many support Modbus, S-net or cloud APIs (Daikin, Mitsubishi, Panasonic)

class HeatPump(BaseDevice):
    device_type = "heat_pump"
    poll_interval = 30

    def __init__(self, ip: str):
        super().__init__()
        self.ip = ip

    async def snapshot(self) -> dict:
        # TODO: Daikin local API, pymodbus for Mitsubishi etc.
        return {
            "mode":           "heat",    # heat | cool | fan | dry | off
            "set_temp_c":     21,
            "indoor_temp_c":  19.5,
            "outdoor_temp_c": 4.2,
            "power_w":        1800,
            "cop":            3.1,       # coefficient of performance
        }

    async def command(self, cmd: str, params: dict) -> dict:
        if cmd == "set_mode":
            return {"queued": f"mode={params.get('mode')}"}
        if cmd == "set_temperature":
            return {"queued": f"temp={params.get('temp_c')}°C"}
        raise NotImplementedError(f"Unknown command '{cmd}'")
