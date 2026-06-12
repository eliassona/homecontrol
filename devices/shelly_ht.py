"""
Shelly H&T Gen3 room sensor plugin — receives data via MQTT.

The H&T Gen3 is battery-powered and sleeps most of the time, so HTTP polling
doesn't work. Instead it pushes readings to an MQTT broker when it wakes up.

SETUP (one-time):
  1. Install Mosquitto on the Pi:
       sudo apt install mosquitto mosquitto-clients
       sudo systemctl enable mosquitto
       sudo systemctl start mosquitto

  2. In the Shelly app or web UI (http://<shelly-ip>):
       Settings -> MQTT -> Enable
       Server: <pi-ip>:1883
       Leave username/password blank unless you configured auth on Mosquitto

  3. Add to config.json (see example below).

MQTT topics published by Shelly H&T Gen3:
  shellies/<device-id>/events/rpc   -- JSON status updates
  shellies/<device-id>/online        -- "true" / "false" (LWT)

Payload example:
  {"src":"shellyhtg3-<mac>","method":"NotifyStatus",
   "params":{"ts":1234567890.0,
     "temperature:0":{"id":0,"tC":22.5,"tF":72.5},
     "humidity:0":{"id":0,"rh":54.3},
     "devicepower:0":{"id":0,"battery":{"V":6.12,"percent":92}}}}

config.json example:
  "living_room_sensor": {
    "type": "shelly_ht",
    "room_name": "Living Room",
    "device_id": "shellyhtg3-aabbcc112233",
    "broker_host": "localhost",
    "broker_port": 1883,
    "stale_minutes": 10
  }

Find device_id in: Shelly app -> device -> Settings -> Device Info -> Device ID
"""

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from devices.registry import BaseDevice

log = logging.getLogger("shelly_ht")


class ShellyHT(BaseDevice):
    device_type = "room_sensor"

    def __init__(
        self,
        device_id: str,
        room_name: str = "",
        room: str = "",         # alias for room_name
        broker_host: str = "",
        broker_port: int = 0,
        mqtt_host: str = "",    # alias for broker_host
        mqtt_port: int = 0,     # alias for broker_port
        stale_minutes: int = 10,
        poll_interval: int = 60,
    ):
        super().__init__()
        self.room_name     = room_name or room or "Unknown"
        self.device_id     = device_id
        self.broker_host   = broker_host or mqtt_host or "localhost"
        self.broker_port   = broker_port or mqtt_port or 1883
        self.stale_minutes = stale_minutes
        self.poll_interval = poll_interval

        self._temperature:  Optional[float] = None
        self._humidity:     Optional[float] = None
        self._battery_pct:  Optional[int]   = None
        self._battery_v:    Optional[float] = None
        self._last_seen:    Optional[datetime] = None
        self._mqtt_client   = None

    def _start_mqtt(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ImportError(
                "paho-mqtt is required: pip install paho-mqtt"
            )

        client = mqtt.Client()

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                log.info(f"[{self.room_name}] MQTT connected to {self.broker_host}:{self.broker_port}")
                # Gen3 topic format: <device-id>/events/rpc (no "shellies/" prefix)
                client.subscribe(f"{self.device_id}/events/rpc")
                client.subscribe(f"{self.device_id}/online")
            else:
                log.error(f"[{self.room_name}] MQTT connection failed rc={rc}")

        def on_message(client, userdata, msg):
            try:
                payload = msg.payload.decode()
                if msg.topic.endswith("/online"):
                    return
                if msg.topic.endswith("/events/rpc"):
                    data   = json.loads(payload)
                    method = data.get("method", "")
                    # Handle both NotifyFullStatus and NotifyStatus
                    if method not in ("NotifyFullStatus", "NotifyStatus"):
                        return
                    params = data.get("params", {})

                    temp = params.get("temperature:0", {})
                    if "tC" in temp:
                        self._temperature = round(temp["tC"], 1)

                    hum = params.get("humidity:0", {})
                    if "rh" in hum:
                        self._humidity = round(hum["rh"], 1)

                    batt = params.get("devicepower:0", {}).get("battery", {})
                    if "percent" in batt:
                        self._battery_pct = batt["percent"]
                    if "V" in batt:
                        self._battery_v = batt["V"]

                    # Only update last_seen if we got actual sensor data
                    if self._temperature is not None:
                        self._last_seen = datetime.now(timezone.utc)
                        log.info(
                            f"[{self.room_name}] "
                            f"{self._temperature}°C  "
                            f"{self._humidity}% RH  "
                            f"battery={self._battery_pct}%"
                        )
            except Exception as e:
                log.warning(f"[{self.room_name}] MQTT parse error: {e}")

        def on_disconnect(client, userdata, rc):
            log.warning(f"[{self.room_name}] MQTT disconnected rc={rc}")

        client.on_connect    = on_connect
        client.on_message    = on_message
        client.on_disconnect = on_disconnect

        try:
            client.connect(self.broker_host, self.broker_port, keepalive=60)
        except Exception as e:
            log.error(f"[{self.room_name}] Cannot connect to broker: {e}")
            return

        self._mqtt_client = client
        t = threading.Thread(
            target=client.loop_forever, daemon=True, name=f"mqtt-{self.device_id}"
        )
        t.start()

    async def start(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._start_mqtt)
        self._poller_task = asyncio.create_task(self._stale_check_loop())

    async def _stale_check_loop(self):
        while True:
            try:
                self._last_snapshot = await self.snapshot()
                self._last_error = None
            except Exception as e:
                self._last_error = str(e)
            await asyncio.sleep(self.poll_interval)

    async def snapshot(self) -> dict:
        now   = datetime.now(timezone.utc)
        stale = True
        if self._last_seen:
            age_min = (now - self._last_seen).total_seconds() / 60
            stale   = age_min > self.stale_minutes

        if self._temperature is None:
            raise ConnectionError(
                f"No data yet from {self.device_id}. "
                "Check Mosquitto is running and MQTT is enabled on the Shelly."
            )

        return {
            "room":        self.room_name,
            "temperature": self._temperature,
            "humidity":    self._humidity,
            "battery_pct": self._battery_pct,
            "battery_v":   self._battery_v,
            "last_seen":   self._last_seen.isoformat() if self._last_seen else None,
            "stale":       stale,
            "device_id":   self.device_id,
        }

    async def stop(self):
        if self._mqtt_client:
            self._mqtt_client.disconnect()
        await super().stop()
