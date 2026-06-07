"""
Outside weather plugin — Open-Meteo API.

Free, no API key, no rate limit for non-commercial use.
Fetches current temperature, humidity, wind, and precipitation
for any lat/lon configured in config.json.

API: https://api.open-meteo.com/v1/forecast
     ?latitude=59.33&longitude=18.07
     &current=temperature_2m,relative_humidity_2m,apparent_temperature,
              precipitation,weather_code,wind_speed_10m,wind_direction_10m
     &wind_speed_unit=ms
     &timezone=Europe/Stockholm
"""

import asyncio
import json
import urllib.request
import urllib.parse
from typing import Optional
from devices.registry import BaseDevice

# WMO weather interpretation codes → human-readable description
# https://open-meteo.com/en/docs#weathervariables
WMO_CODES = {
    0:  "Clear sky",
    1:  "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}


def _fetch_json(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HomeControl/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


class OutsideWeather(BaseDevice):
    """
    Current outside weather from Open-Meteo for a configured location.

    snapshot() returns:
      temperature_c       — current 2m air temperature
      feels_like_c        — apparent temperature
      humidity_pct        — relative humidity %
      precipitation_mm    — precipitation in last hour (mm)
      wind_speed_ms       — wind speed m/s
      wind_direction_deg  — wind direction (degrees)
      wind_direction_str  — e.g. "SW"
      weather_code        — WMO code
      weather_desc        — human-readable e.g. "Partly cloudy"
      observed_at         — ISO timestamp from API
      location_name       — as set in config
      latitude / longitude
    """

    device_type = "outside_weather"

    def __init__(
        self,
        latitude: float,
        longitude: float,
        location_name: str = "",
        poll_interval: int = 300,   # 5 minutes — Open-Meteo updates every 15 min
    ):
        super().__init__()
        self.latitude = latitude
        self.longitude = longitude
        self.location_name = location_name or f"{latitude},{longitude}"
        self.poll_interval = poll_interval

    def _build_url(self) -> str:
        params = urllib.parse.urlencode({
            "latitude":     self.latitude,
            "longitude":    self.longitude,
            "current":      ",".join([
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
                "wind_direction_10m",
            ]),
            "wind_speed_unit": "ms",
            "timezone":     "Europe/Stockholm",
        })
        return f"https://api.open-meteo.com/v1/forecast?{params}"

    @staticmethod
    def _degrees_to_compass(deg: Optional[float]) -> str:
        if deg is None:
            return "?"
        directions = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                      "S","SSW","SW","WSW","W","WNW","NW","NNW"]
        idx = round(deg / 22.5) % 16
        return directions[idx]

    async def snapshot(self) -> dict:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_json, self._build_url())

        if not data or "current" not in data:
            raise ConnectionError("No response from Open-Meteo API")

        c = data["current"]
        code = c.get("weather_code")

        return {
            "temperature_c":      c.get("temperature_2m"),
            "feels_like_c":       c.get("apparent_temperature"),
            "humidity_pct":       c.get("relative_humidity_2m"),
            "precipitation_mm":   c.get("precipitation"),
            "wind_speed_ms":      c.get("wind_speed_10m"),
            "wind_direction_deg": c.get("wind_direction_10m"),
            "wind_direction_str": self._degrees_to_compass(c.get("wind_direction_10m")),
            "weather_code":       code,
            "weather_desc":       WMO_CODES.get(code, f"Code {code}"),
            "observed_at":        c.get("time"),
            "location_name":      self.location_name,
            "latitude":           self.latitude,
            "longitude":          self.longitude,
        }
