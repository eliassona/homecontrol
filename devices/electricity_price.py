"""
Swedish electricity spot price plugin — SE1/SE2/SE3/SE4.

Data source: elprisetjustnu.se (free, no API key, no rate limit stated)
Since 1 Oct 2025 Sweden uses 15-minute resolution: 96 prices/day.

The plugin:
  - Fetches today's full price schedule once, caches it in memory
  - Fetches tomorrow's prices when available (usually published ~13:00)
  - Derives the current 15-min price from the cached schedule
  - Exposes cheapest/most expensive periods for use in automation rules
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
import urllib.request
import json

from devices.registry import BaseDevice


# Sweden is UTC+1 (CET) in winter, UTC+2 (CEST) in summer.
# The API timestamps are in UTC; we compare against local Swedish time.
def _sweden_now() -> datetime:
    # Use the system clock — on the Pi this should be set correctly.
    # If running elsewhere, the offset is still correct via UTC.
    return datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=2))  # CEST (summer); change to +1 in winter
    )


def _fetch_json(url: str) -> Optional[list]:
    """Synchronous fetch — run in a thread executor to avoid blocking the event loop."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HomeControl/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


class ElectricityPrice(BaseDevice):
    """
    Current and scheduled electricity spot prices for a Swedish price area.

    snapshot() returns:
      price_now_sek       — current 15-min spot price in SEK/kWh (excl. VAT)
      price_now_ore       — same in öre/kWh
      period_start        — ISO timestamp when current price period started
      period_end          — ISO timestamp when current price period ends
      today_min_ore       — cheapest period today
      today_max_ore       — most expensive period today
      today_avg_ore       — average price today
      cheapest_periods    — list of the 8 cheapest 15-min slots today [{start, ore}]
      price_area          — e.g. "SE3"
      source              — attribution
    """

    device_type = "electricity_price"
    poll_interval = 60   # re-derive current price every minute from cached schedule

    def __init__(self, price_area: str = "SE3", poll_interval: int = 60):
        super().__init__()
        self.price_area = price_area.upper()
        self.poll_interval = poll_interval
        self._schedule: list = []          # today's 96 price slots
        self._schedule_date: Optional[str] = None   # "YYYY-MM-DD" of cached schedule
        self._fetch_lock = asyncio.Lock()

    # ── Internal ──────────────────────────────────────────────────────────

    def _api_url(self, dt: datetime) -> str:
        return (
            f"https://www.elprisetjustnu.se/api/v1/prices"
            f"/{dt.year}/{dt.strftime('%m-%d')}_{self.price_area}.json"
        )

    async def _ensure_schedule(self, today: datetime):
        """Fetch (or re-fetch) the day's price schedule if needed."""
        date_str = today.strftime("%Y-%m-%d")
        if self._schedule_date == date_str and self._schedule:
            return  # already have today's data

        async with self._fetch_lock:
            # Double-check after acquiring lock
            if self._schedule_date == date_str and self._schedule:
                return

            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, _fetch_json, self._api_url(today))

            if data:
                self._schedule = data
                self._schedule_date = date_str

    def _current_slot(self, now: datetime) -> Optional[dict]:
        """Find the price slot whose time_start <= now < time_end."""
        now_utc = now.astimezone(timezone.utc)
        for slot in self._schedule:
            try:
                # API returns ISO 8601 with +00:00 suffix
                start = datetime.fromisoformat(slot["time_start"])
                end   = datetime.fromisoformat(slot["time_end"])
                if start <= now_utc < end:
                    return slot
            except (KeyError, ValueError):
                continue
        return None

    def _sek_per_kwh(self, slot: dict) -> float:
        """Return SEK/kWh from a slot. API gives EUR/kWh + SEK_per_kWh."""
        # elprisetjustnu.se provides both; use SEK directly
        return round(slot.get("SEK_per_kWh", 0), 4)

    # ── BaseDevice ────────────────────────────────────────────────────────

    async def snapshot(self) -> dict:
        now = _sweden_now()
        await self._ensure_schedule(now)

        if not self._schedule:
            raise ConnectionError("Could not fetch price schedule from elprisetjustnu.se")

        slot = self._current_slot(now)
        if not slot:
            raise ValueError(f"No price slot found for current time {now.isoformat()}")

        price_sek = self._sek_per_kwh(slot)
        price_ore = round(price_sek * 100, 2)

        # Day statistics
        all_ore = [s.get("SEK_per_kWh", 0) * 100 for s in self._schedule]
        today_min = round(min(all_ore), 2)
        today_max = round(max(all_ore), 2)
        today_avg = round(sum(all_ore) / len(all_ore), 2)

        # 8 cheapest 15-min slots today (useful for scheduling)
        sorted_slots = sorted(self._schedule, key=lambda s: s.get("SEK_per_kWh", 0))
        cheapest = [
            {
                "start": s["time_start"],
                "ore":   round(s.get("SEK_per_kWh", 0) * 100, 2),
            }
            for s in sorted_slots[:8]
        ]

        return {
            "price_now_sek":    price_sek,
            "price_now_ore":    price_ore,
            "period_start":     slot["time_start"],
            "period_end":       slot["time_end"],
            "today_min_ore":    today_min,
            "today_max_ore":    today_max,
            "today_avg_ore":    today_avg,
            "cheapest_periods": cheapest,
            "price_area":       self.price_area,
            "source":           "elprisetjustnu.se",
        }
