"""
Automation rules engine.

Rules are evaluated on a schedule and act on devices via the registry.
Each rule is a self-contained async class with evaluate() → Optional[bool].

Adding a new rule:
  1. Subclass BaseRule, implement evaluate() and apply()
  2. Register it in RULE_CLASSES at the bottom
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, List

log = logging.getLogger("automator")


# ── Base rule ────────────────────────────────────────────────────────────────

class BaseRule:
    """
    Periodically evaluates conditions and decides whether a target device
    should be on (True), off (False), or unchanged (None — dead zone).
    """

    name: str = "unnamed_rule"
    interval_seconds: int = 3600

    def __init__(self, registry, cfg: dict):
        self.registry = registry
        self.cfg = cfg
        self._task: Optional[asyncio.Task] = None
        self._manual: bool = False

    async def evaluate(self) -> Optional[bool]:
        raise NotImplementedError

    async def apply(self, should_run: bool):
        raise NotImplementedError

    async def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        # Wait for devices to complete their first poll before evaluating
        await asyncio.sleep(30)
        while True:
            try:
                await self._tick()
            except Exception as e:
                log.error(f"[{self.name}] Error: {e}")
            await asyncio.sleep(self.interval_seconds)

    async def _tick(self):
        if getattr(self, '_manual', False):
            log.debug(f"[{self.name}] Manual mode — skipping evaluation")
            return
        decision = await self.evaluate()
        if decision is None:
            log.info(f"[{self.name}] Dead zone — leaving state unchanged")
            return
        log.info(f"[{self.name}] → {'RUN' if decision else 'PAUSE'} "
                 f"at {datetime.now().strftime('%H:%M:%S')}")
        await self.apply(decision)


# ── Shared miner apply logic ──────────────────────────────────────────────────

class MinerRule(BaseRule):
    """Base for any rule that controls the S9 miner."""

    def __init__(self, registry, cfg: dict):
        super().__init__(registry, cfg)
        self.miner_id = cfg.get("miner_device", "s9_miner")
        self.interval_seconds = int(cfg.get("interval_seconds", 3600))

    async def apply(self, should_run: bool):
        miner = self.registry.get(self.miner_id)
        if not miner:
            log.warning(f"[{self.name}] Miner '{self.miner_id}' not found")
            return
        try:
            currently_mining = await miner.is_mining()
        except Exception as e:
            log.error(f"[{self.name}] Could not read miner state: {e}")
            return

        if should_run and not currently_mining:
            log.info(f"[{self.name}] ▶ Resuming miner")
            await miner.command("resume", {})
        elif not should_run and currently_mining:
            log.info(f"[{self.name}] ⏸ Pausing miner")
            await miner.command("pause", {})
        else:
            log.info(f"[{self.name}] Miner already {'running' if currently_mining else 'paused'}, no change")


# ── Miner temp + price rule ───────────────────────────────────────────────────

class MinerTempPriceRule(MinerRule):
    """
    Controls the S9 miner based on outside temperature AND electricity price.

    Logic (with hysteresis of `hys` öre on price, fixed ±1°C on temp):

      RESUME when:  temp < temp_on   AND  price < day_avg - hys
      PAUSE  when:  temp > temp_off  OR   price > day_avg + hys
      HOLD   otherwise (dead zone — keep current state)

    Config keys (all optional, defaults shown):
      miner_device:      "s9_miner"
      weather_device:    "outside_weather"
      price_device:      "electricity_price"
      temp_on:           20.0     °C  — resume threshold
      temp_off:          22.0     °C  — pause threshold
      price_hys_ore:     20.0     öre — hysteresis band around daily average
      interval_seconds:  3600
    """

    name = "miner_temp_price_rule"

    def __init__(self, registry, cfg: dict):
        super().__init__(registry, cfg)
        self.weather_id        = cfg.get("weather_device",   "outside_weather")
        self.price_id          = cfg.get("price_device",     "electricity_price")
        self.verisure_id       = cfg.get("verisure_device",  "verisure")
        self.temp_on           = float(cfg.get("temp_on",          20.0))
        self.temp_off          = float(cfg.get("temp_off",         22.0))
        self.price_hys         = float(cfg.get("price_hys_ore",    20.0))
        self.indoor_sensor       = cfg.get("indoor_sensor")           # e.g. "Hall, Entré"
        self.indoor_temp_on      = float(cfg.get("indoor_temp_on",      22.0))
        self.indoor_temp_off     = float(cfg.get("indoor_temp_off",     24.0))
        self.indoor_heat_offset  = float(cfg.get("indoor_heat_offset",   0.5))
        # If indoor_temp < indoor_temp_on - indoor_heat_offset → resume regardless of price

    def _get_indoor_temp(self) -> Optional[float]:
        """
        Look up the configured climate sensor from the Verisure snapshot.
        Returns the temperature, or None if sensor is not configured.
        Raises RuntimeError if configured but unavailable/not found — caller
        should treat this as a blocking condition (hold state).
        """
        if not self.indoor_sensor:
            return None   # not configured — condition is ignored entirely

        verisure = self.registry.get(self.verisure_id)
        if not verisure:
            raise RuntimeError(f"Verisure device '{self.verisure_id}' not registered")

        snap = verisure.cached_snapshot()
        if not snap.get("online"):
            raise RuntimeError(f"Verisure offline (error: {snap.get('error', 'unknown')})")

        climates = snap.get("climate", [])
        if not climates:
            raise RuntimeError("Verisure has no climate data yet — still polling?")

        for c in climates:
            if c.get("name", "").lower() == self.indoor_sensor.lower():
                temp = c.get("temperature")
                if temp is None:
                    raise RuntimeError(f"Sensor '{self.indoor_sensor}' found but has no temperature value")
                return temp

        available = [c.get("name") for c in climates]
        raise RuntimeError(
            f"Indoor sensor '{self.indoor_sensor}' not found. "
            f"Available: {available}"
        )

    async def evaluate(self) -> Optional[bool]:
        # ── Fetch outside weather snapshot ───────────────────────────────────
        weather = self.registry.get(self.weather_id)
        if not weather or not weather.cached_snapshot().get("online"):
            log.warning(f"[{self.name}] Weather offline — holding state")
            return None

        temp = weather.cached_snapshot().get("temperature_c")
        if temp is None:
            log.warning(f"[{self.name}] No temperature data — holding state")
            return None

        # ── Fetch electricity price snapshot ─────────────────────────────────
        price_dev = self.registry.get(self.price_id)
        if not price_dev or not price_dev.cached_snapshot().get("online"):
            log.warning(f"[{self.name}] Price device offline — holding state")
            return None

        price_snap    = price_dev.cached_snapshot()
        price_now     = price_snap.get("price_now_ore")
        price_avg     = price_snap.get("today_avg_ore")

        if price_now is None or price_avg is None:
            log.warning(f"[{self.name}] No price data — holding state")
            return None

        threshold_on  = price_avg - self.price_hys
        threshold_off = price_avg + self.price_hys

        # ── Fetch indoor temperature (optional) ──────────────────────────────
        indoor_temp = None
        indoor_configured = bool(self.indoor_sensor)
        try:
            indoor_temp = self._get_indoor_temp()
        except RuntimeError as e:
            if indoor_configured:
                # Sensor is configured but unavailable — hold, don't guess
                log.warning(f"[{self.name}] Indoor temp unavailable: {e} — holding state")
                return None

        log.info(
            f"[{self.name}] "
            f"outside={temp}°C  "
            f"price={price_now:.1f} öre  avg={price_avg:.1f} öre  "
            f"on<{threshold_on:.1f}  off>{threshold_off:.1f}"
            + (f"  indoor={indoor_temp}°C" if indoor_temp is not None else "  indoor=n/a")
        )

        # ── Decision ─────────────────────────────────────────────────────────
        #   Check outdoor temp
        outside_good = temp < self.temp_on
        outside_bad  = temp > self.temp_off

        #   Check price
        price_good = price_now < threshold_on
        price_bad  = price_now > threshold_off

        #   Check indoor temp (only if sensor is configured)
        too_cold = False
        if indoor_configured and indoor_temp is not None:
            indoor_good = indoor_temp < self.indoor_temp_on
            indoor_bad  = indoor_temp > self.indoor_temp_off
            # "Too cold" override: resume regardless of price if room needs heating
            heat_threshold = self.indoor_temp_on - self.indoor_heat_offset
            too_cold = indoor_temp < heat_threshold
            if too_cold:
                log.info(
                    f"[{self.name}] ❄ Too cold override: "
                    f"indoor={indoor_temp}°C < {heat_threshold}°C — resuming regardless of price"
                )
        else:
            indoor_good = True   # not configured → don't block
            indoor_bad  = False

        #   RESUME: too cold override (ignores price, still respects outside temp)
        if too_cold and outside_good:
            return True

        #   RESUME: all conditions clearly favourable
        if outside_good and price_good and indoor_good:
            return True

        #   PAUSE: any single condition clearly unfavourable
        if outside_bad or price_bad or indoor_bad:
            return False

        #   Dead zone — hold current state
        return None


# ── Registry ──────────────────────────────────────────────────────────────────

RULE_CLASSES = {
    "miner_temp_price_rule": MinerTempPriceRule,
}


class Automator:
    """Loads and runs automation rules defined in config.json → automation.rules."""

    def __init__(self, registry, rules_cfg: List[dict]):
        self._rules: List[BaseRule] = []
        self._manual = False
        for rule_cfg in rules_cfg:
            rule_type = rule_cfg.get("type")
            cls = RULE_CLASSES.get(rule_type)
            if not cls:
                log.warning(f"Unknown rule type '{rule_type}', skipping")
                continue
            rule = cls(registry, rule_cfg)
            self._rules.append(rule)
            log.info(f"Loaded rule: {rule.name} (interval={rule.interval_seconds}s)")

    def set_manual(self, manual: bool):
        self._manual = manual
        for rule in self._rules:
            rule._manual = manual
        log.info(f"Automation mode: {'MANUAL' if manual else 'AUTO'}")

    async def start(self):
        for rule in self._rules:
            await rule.start()

    async def stop(self):
        for rule in self._rules:
            await rule.stop()
