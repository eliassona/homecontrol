"""
Device plugin system.
Every device inherits from BaseDevice and implements snapshot() + command().
"""

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional


class BaseDevice(ABC):
    """
    Base class for all home automation devices.

    Subclass this and implement:
      - snapshot()  → dict with current readings
      - command()   → handle control commands

    The poller calls snapshot() every `poll_interval` seconds automatically.
    """

    device_type: str = "unknown"
    poll_interval: int = 10  # seconds — override per device

    def __init__(self, name: str = ""):
        self.name = name or self.__class__.__name__
        self._last_snapshot: dict = {}
        self._last_error: Optional[str] = None
        self._poller_task: Optional[asyncio.Task] = None

    @abstractmethod
    async def snapshot(self) -> dict:
        """Return current readings as a flat dict. Must be JSON-serialisable."""
        ...

    async def command(self, cmd: str, params: dict) -> Any:
        """Override to handle control commands. Default: raise not implemented."""
        raise NotImplementedError(f"Device '{self.name}' does not support commands")

    async def start(self):
        """Start background polling."""
        self._poller_task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        if self._poller_task:
            self._poller_task.cancel()

    async def _poll_loop(self):
        while True:
            try:
                self._last_snapshot = await self.snapshot()
                self._last_error = None
            except Exception as e:
                self._last_error = str(e)
            await asyncio.sleep(self.poll_interval)

    def cached_snapshot(self) -> dict:
        """Return last polled snapshot with metadata."""
        return {
            "device_id": self.name,
            "device_type": self.device_type,
            "timestamp": datetime.utcnow().isoformat(),
            "online": self._last_error is None,
            "error": self._last_error,
            **self._last_snapshot,
        }


class DeviceRegistry:
    def __init__(self):
        self._devices: dict[str, BaseDevice] = {}

    def register(self, device_id: str, device: BaseDevice):
        device.name = device_id
        self._devices[device_id] = device

    def get(self, device_id: str) -> Optional[BaseDevice]:
        return self._devices.get(device_id)

    def list_devices(self) -> list[dict]:
        return [
            {"id": k, "type": v.device_type, "name": v.name}
            for k, v in self._devices.items()
        ]

    async def start_all(self):
        for device in self._devices.values():
            await device.start()

    async def stop_all(self):
        for device in self._devices.values():
            await device.stop()

    async def snapshot_all(self) -> dict:
        return {k: v.cached_snapshot() for k, v in self._devices.items()}
