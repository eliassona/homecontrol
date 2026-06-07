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
        self.weather_id   = cfg.get("weather_device",  "outside_weather")
        self.price_id     = cfg.get("price_device",    "electricity_price")
        self.temp_on      = float(cfg.get("temp_on",         20.0))
        self.temp_off     = float(cfg.get("temp_off",        22.0))
        self.price_hys    = float(cfg.get("price_hys_ore",   20.0))

    async def evaluate(self) -> Optional[bool]:
        # ── Fetch weather snapshot ───────────────────────────────────────────
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

        price_snap  = price_dev.cached_snapshot()
        price_now   = price_snap.get("price_now_ore")
        price_avg   = price_snap.get("today_avg_ore")

        if price_now is None or price_avg is None:
            log.warning(f"[{self.name}] No price data — holding state")
            return None

        threshold_on  = price_avg - self.price_hys   # cheap enough to mine
        threshold_off = price_avg + self.price_hys   # too expensive to mine

        log.info(
            f"[{self.name}] temp={temp}°C  "
            f"price={price_now:.1f} öre  avg={price_avg:.1f} öre  "
            f"on<{threshold_on:.1f}  off>{threshold_off:.1f}"
        )

        # ── Decision ─────────────────────────────────────────────────────────
        #   RESUME: temp AND price both clearly favourable
        if temp < self.temp_on and price_now < threshold_on:
            return True

        #   PAUSE: temp too high OR price too high
        if temp > self.temp_off or price_now > threshold_off:
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
        for rule_cfg in rules_cfg:
            rule_type = rule_cfg.get("type")
            cls = RULE_CLASSES.get(rule_type)
            if not cls:
                log.warning(f"Unknown rule type '{rule_type}', skipping")
                continue
            rule = cls(registry, rule_cfg)
            self._rules.append(rule)
            log.info(f"Loaded rule: {rule.name} (interval={rule.interval_seconds}s)")

    async def start(self):
        for rule in self._rules:
            await rule.start()

    async def stop(self):
        for rule in self._rules:
            await rule.stop()
