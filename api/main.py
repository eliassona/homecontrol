"""
HomeControl API — Raspberry Pi Home Automation
FastAPI backend. Each device is a plugin in devices/.
Config is loaded from config.json at the project root.
"""

import json
import logging
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from datetime import datetime

from devices.registry import DeviceRegistry
from devices.s9_miner import S9Miner
from devices.electricity_price import ElectricityPrice
from devices.outside_weather import OutsideWeather
from devices.verisure import VerisureSystem
from api.automator import Automator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

# ── Config ───────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent.parent / "config.json"

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)

cfg = load_config()

# ── Device factory ────────────────────────────────────────────────────────────

DEVICE_CLASSES = {
    "s9_miner":          S9Miner,
    "electricity_price": ElectricityPrice,
    "outside_weather":   OutsideWeather,
    "verisure":          VerisureSystem,
    # "room_sensor":   RoomSensor,
    # "power_meter":   PowerMeter,
    # "tesla_charger": TeslaCharger,
    # "wifi_radiator": WifiRadiator,
    # "heat_pump":     HeatPump,
}

def build_device(device_cfg: dict):
    device_type = device_cfg["type"]
    cls = DEVICE_CLASSES.get(device_type)
    if not cls:
        raise ValueError(f"Unknown device type '{device_type}'. "
                         f"Add it to DEVICE_CLASSES in main.py.")
    kwargs = {k: v for k, v in device_cfg.items() if k != "type"}
    return cls(**kwargs)

# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="HomeControl API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

registry  = DeviceRegistry()
automator = None

@app.on_event("startup")
async def startup():
    global automator

    for device_id, device_cfg in cfg.get("devices", {}).items():
        device = build_device(device_cfg)
        registry.register(device_id, device)
        print(f"  Registered: {device_id} ({device_cfg['type']})")

    await registry.start_all()

    rules_cfg = cfg.get("automation", {}).get("rules", [])
    automator = Automator(registry, rules_cfg)
    await automator.start()

    print(f"HomeControl running — {len(registry.list_devices())} device(s), "
          f"{len(rules_cfg)} rule(s)")

@app.on_event("shutdown")
async def shutdown():
    if automator:
        await automator.stop()
    await registry.stop_all()

# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "devices": await registry.snapshot_all(),
    }

@app.get("/api/devices")
async def list_devices():
    return registry.list_devices()

@app.get("/api/devices/{device_id}")
async def get_device(device_id: str):
    device = registry.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    return await device.snapshot()

@app.post("/api/devices/{device_id}/command")
async def send_command(device_id: str, payload: dict):
    """Send a command to a device. Payload: {"command": "...", "params": {...}}"""
    device = registry.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    result = await device.command(payload.get("command"), payload.get("params", {}))
    return {"ok": True, "result": result}

@app.get("/api/automation")
async def get_automation():
    """Return automation rule config (for dashboard display)."""
    return cfg.get("automation", {})

@app.get("/api/config")
async def get_config():
    return {
        "dashboard":  cfg.get("dashboard", {}),
        "devices":    list(cfg.get("devices", {}).keys()),
        "automation": cfg.get("automation", {}),
    }

# ── Dashboard ─────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def dashboard():
    return FileResponse("static/index.html")
