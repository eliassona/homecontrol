"""
Home Assistant device plugin.

Talks to a local HA instance via the REST API (long-lived access token).
One connection can expose multiple appliances (oven, dishwasher, etc.).

config.json keys:
  host           Base URL, e.g. "http://192.168.1.175:8123"
  token          Long-lived access token
  poll_interval  Seconds between polls (default: 15)
  appliances     List of appliance definitions:
    id           Short id used in the UI (e.g. "oven")
    name         Friendly label
    entities     Map of role → entity_id, e.g.
                   power, child_lock, operation_state, door, progress,
                   finish_time, temperature, selected_programme,
                   active_programme, stop, resume, connectivity,
                   remote_control, remote_start, duration, ...
                   plus any extra switch/select/number/button roles

snapshot() returns:
  appliances: {
    "<id>": {
      name, online, power, child_lock, operation_state, door,
      progress, finish_time, temperature, selected_programme,
      active_programme, options (for selects), ...raw entity states
    }
  }

command() supports:
  on / off          params: {entity_id} or {appliance, role}
  toggle            params: {entity_id} or {appliance, role}
  set_option        params: {entity_id|appliance+role, option}
  set_value         params: {entity_id|appliance+role, value}
  press             params: {entity_id} or {appliance, role}
  set               params: {entity_id, domain, ...}  (generic)
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Optional

from devices.registry import BaseDevice


def _ha_request(host: str, token: str, path: str, method: str = "GET",
                body: Optional[dict] = None) -> Any:
    url = host.rstrip("/") + path
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "HomeControl/1.0",
    }
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise ConnectionError(f"HA {method} {path} → {e.code}: {err_body}") from e
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach Home Assistant at {host}: {e}") from e


def _is_on(state: str) -> bool:
    return state in ("on", "true", "1", "open", "unlocked")


def _humanize_option(opt: str) -> str:
    """cooking_oven_program_heating_mode_hot_air → Hot air"""
    if not opt or opt in ("unknown", "unavailable", "none"):
        return opt or "—"
    s = opt
    for prefix in (
        "dishcare_dishwasher_program_",
        "cooking_oven_program_heating_mode_",
        "cooking_oven_program_",
        "dishcare_dishwasher_",
        "cooking_oven_",
    ):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s.replace("_", " ").strip().capitalize()


class HomeAssistant(BaseDevice):
    device_type = "home_assistant"

    def __init__(
        self,
        host: str,
        token: str,
        appliances: Optional[list] = None,
        poll_interval: int = 15,
        **_extra,
    ):
        super().__init__()
        self.host = host.rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.token = token
        self.poll_interval = poll_interval
        self.appliances_cfg = appliances or []
        # role → entity_id lookup across all appliances
        self._entity_roles: dict[str, tuple[str, str]] = {}  # entity_id → (app_id, role)
        self._all_entity_ids: list[str] = []
        for app in self.appliances_cfg:
            app_id = app["id"]
            for role, eid in (app.get("entities") or {}).items():
                if not eid:
                    continue
                self._entity_roles[eid] = (app_id, role)
                self._all_entity_ids.append(eid)

    def _resolve_entity(self, params: dict) -> str:
        if params.get("entity_id"):
            return params["entity_id"]
        app_id = params.get("appliance")
        role = params.get("role")
        if app_id and role:
            for app in self.appliances_cfg:
                if app["id"] == app_id:
                    eid = (app.get("entities") or {}).get(role)
                    if eid:
                        return eid
        raise ValueError("command requires entity_id or appliance+role")

    async def snapshot(self) -> dict:
        loop = asyncio.get_event_loop()
        states = await loop.run_in_executor(None, self._fetch_states)
        by_id = {s["entity_id"]: s for s in states}

        appliances: dict[str, dict] = {}
        for app in self.appliances_cfg:
            app_id = app["id"]
            entities = app.get("entities") or {}
            out: dict[str, Any] = {
                "id": app_id,
                "name": app.get("name") or app_id,
                "online": True,
            }
            raw_states: dict[str, Any] = {}
            options_map: dict[str, list] = {}

            for role, eid in entities.items():
                st = by_id.get(eid)
                if st is None:
                    raw_states[role] = {"entity_id": eid, "state": "unavailable"}
                    continue
                state = st.get("state", "unavailable")
                attrs = st.get("attributes") or {}
                raw_states[role] = {
                    "entity_id": eid,
                    "state": state,
                    "attributes": {
                        k: attrs[k]
                        for k in (
                            "friendly_name", "unit_of_measurement", "options",
                            "min", "max", "step", "device_class",
                        )
                        if k in attrs
                    },
                }
                if "options" in attrs:
                    options_map[role] = attrs["options"]

                # Convenience fields for common roles
                if role == "power":
                    out["power"] = _is_on(state) if state not in ("unavailable", "unknown") else None
                elif role == "child_lock":
                    out["child_lock"] = _is_on(state) if state not in ("unavailable", "unknown") else None
                elif role == "operation_state":
                    out["operation_state"] = state if state not in ("unavailable", "unknown") else None
                elif role == "door":
                    out["door"] = state if state not in ("unavailable", "unknown") else None
                elif role == "progress":
                    try:
                        out["progress"] = float(state) if state not in ("unavailable", "unknown") else None
                    except (TypeError, ValueError):
                        out["progress"] = None
                elif role == "finish_time":
                    out["finish_time"] = state if state not in ("unavailable", "unknown") else None
                elif role == "temperature":
                    try:
                        out["temperature"] = float(state) if state not in ("unavailable", "unknown") else None
                    except (TypeError, ValueError):
                        out["temperature"] = None
                elif role == "selected_programme":
                    out["selected_programme"] = state if state not in ("unavailable", "unknown") else None
                elif role == "active_programme":
                    out["active_programme"] = state if state not in ("unavailable", "unknown") else None
                elif role == "connectivity":
                    out["online"] = _is_on(state)
                elif role == "remote_control":
                    out["remote_control"] = _is_on(state)
                elif role == "remote_start":
                    out["remote_start"] = _is_on(state)
                elif role == "duration":
                    try:
                        out["duration"] = float(state) if state not in ("unavailable", "unknown") else None
                    except (TypeError, ValueError):
                        out["duration"] = None
                else:
                    # Extra switches / sensors — expose under their role name
                    domain = eid.split(".", 1)[0]
                    if domain == "switch":
                        out[role] = _is_on(state) if state not in ("unavailable", "unknown") else None
                    elif domain in ("sensor", "binary_sensor", "select", "number"):
                        out[role] = state if state not in ("unavailable", "unknown") else None

            out["entities"] = raw_states
            out["options"] = options_map
            # Human-readable labels for programme options
            for role, opts in options_map.items():
                out.setdefault("option_labels", {})[role] = {
                    o: _humanize_option(o) for o in opts
                }
            appliances[app_id] = out

        return {"appliances": appliances}

    def _fetch_states(self) -> list:
        # Fetch all states once (cheap) and filter — more reliable than N GETs
        all_states = _ha_request(self.host, self.token, "/api/states")
        if not isinstance(all_states, list):
            return []
        wanted = set(self._all_entity_ids)
        return [s for s in all_states if s.get("entity_id") in wanted]

    async def command(self, cmd: str, params: dict) -> dict:
        loop = asyncio.get_event_loop()
        params = params or {}
        entity_id = self._resolve_entity(params)
        domain = entity_id.split(".", 1)[0]

        if cmd in ("on", "turn_on"):
            await loop.run_in_executor(
                None, _ha_request, self.host, self.token,
                f"/api/services/{domain}/turn_on", "POST",
                {"entity_id": entity_id},
            )
            return {"ok": True}

        if cmd in ("off", "turn_off"):
            await loop.run_in_executor(
                None, _ha_request, self.host, self.token,
                f"/api/services/{domain}/turn_off", "POST",
                {"entity_id": entity_id},
            )
            return {"ok": True}

        if cmd == "toggle":
            await loop.run_in_executor(
                None, _ha_request, self.host, self.token,
                f"/api/services/{domain}/toggle", "POST",
                {"entity_id": entity_id},
            )
            return {"ok": True}

        if cmd == "set_option":
            option = params.get("option")
            if option is None:
                raise ValueError("set_option requires 'option'")
            await loop.run_in_executor(
                None, _ha_request, self.host, self.token,
                "/api/services/select/select_option", "POST",
                {"entity_id": entity_id, "option": option},
            )
            return {"ok": True}

        if cmd == "set_value":
            value = params.get("value")
            if value is None:
                raise ValueError("set_value requires 'value'")
            await loop.run_in_executor(
                None, _ha_request, self.host, self.token,
                "/api/services/number/set_value", "POST",
                {"entity_id": entity_id, "value": value},
            )
            return {"ok": True}

        if cmd == "press":
            await loop.run_in_executor(
                None, _ha_request, self.host, self.token,
                "/api/services/button/press", "POST",
                {"entity_id": entity_id},
            )
            return {"ok": True}

        raise NotImplementedError(f"Unknown command '{cmd}'")
