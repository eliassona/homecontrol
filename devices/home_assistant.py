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
  start_program     params: {appliance, programme?, duration_s?, options?{role: value}}
                    Returns immediately and runs in the background: door check →
                    power on → select programme → set options → start → verify.
                    Waits on real HA state (no fixed sleeps).
  start_status      params: {appliance} → {"ok": True, "status": {state, text, ts}}
                    state is "running", "ok" or "error"
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from devices.registry import BaseDevice

log = logging.getLogger("home_assistant")


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
        self._start_status: dict[str, dict] = {}   # appliance id → {state, text, ts}
        self._start_tasks: dict[str, asyncio.Task] = {}
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

    # ── start_program: reliable multi-step start ─────────────────────────────

    async def _state(self, eid: str) -> dict:
        loop = asyncio.get_event_loop()
        try:
            st = await loop.run_in_executor(
                None, _ha_request, self.host, self.token, f"/api/states/{eid}")
            return st if isinstance(st, dict) else {"state": "unavailable", "attributes": {}}
        except ConnectionError:
            return {"state": "unavailable", "attributes": {}}

    async def _call(self, domain: str, service: str, data: dict) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, _ha_request, self.host, self.token,
            f"/api/services/{domain}/{service}", "POST", data)

    async def _wait_for(self, eid: str, pred, timeout: float = 10.0, interval: float = 0.5):
        """Poll an entity until pred(state) is true. Returns the state dict, or None on timeout."""
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while True:
            st = await self._state(eid)
            if pred(st.get("state")):
                return st
            if loop.time() >= end:
                return None
            await asyncio.sleep(interval)

    def _set_status(self, app_id: str, state: str, text: str) -> None:
        log.info("start_program[%s] %s: %s", app_id, state, text)
        self._start_status[app_id] = {"state": state, "text": text, "ts": time.time()}

    async def _start_runner(self, params: dict) -> None:
        """Runs _start_program in the background and records the outcome for start_status."""
        app_id = params.get("appliance")
        try:
            res = await self._start_program(params)
            if res.get("ok"):
                self._set_status(app_id, "ok", "Started")
            else:
                self._set_status(app_id, "error", res.get("error") or "Failed")
        except Exception as e:  # never let the task die silently
            log.exception("start_program crashed")
            self._set_status(app_id, "error", f"Backend error: {e}")

    async def _start_program(self, params: dict) -> dict:
        def fail(msg: str) -> dict:
            log.warning("start_program failed: %s", msg)
            return {"ok": False, "error": msg}

        BAD = (None, "unavailable", "unknown")
        app_id = params.get("appliance")
        app = next((a for a in self.appliances_cfg if a["id"] == app_id), None)
        if not app:
            return fail(f"Unknown appliance '{app_id}'")
        ents = app.get("entities") or {}
        name = app.get("name") or app_id

        def step(text: str) -> None:
            self._set_status(app_id, "running", text)

        # 1. Door must not be open
        step("Checking door…")
        if ents.get("door"):
            st = await self._state(ents["door"])
            if st.get("state") == "open":
                return fail(f"{name} door is open")

        # 2. Power on
        if ents.get("power"):
            st = await self._state(ents["power"])
            if st.get("state") != "on":
                step("Powering on…")
                await self._call("switch", "turn_on", {"entity_id": ents["power"]})
                if await self._wait_for(ents["power"], lambda s: s == "on", 10) is None:
                    return fail(f"{name} did not power on")

        # 3. Remote start flag. Not a hard requirement here (some appliances start fine while it
        #    reports off), so only remember it to give a better hint if the start fails.
        remote_ok = True
        if ents.get("remote_start"):
            step("Checking remote start…")
            remote_ok = await self._wait_for(ents["remote_start"], lambda s: s == "on", 4) is not None
            if not remote_ok:
                log.info("remote_start reports off for %s – trying anyway", app_id)
        hint = "" if remote_ok else " (remote start reports off – close the door and enable remote start on the appliance)"

        # 4. Programme
        sel = ents.get("selected_programme")
        act = ents.get("active_programme")
        if not sel or not act:
            return fail(f"{name} has no programme entities configured")
        programme = params.get("programme")
        if programme:
            # Always send the selection, even if it already shows this programme: re-selecting
            # makes Home Connect reload the programme's options (duration, temperature, ...).
            # Without it those number entities can stay "unavailable".
            step("Selecting programme…")
            await self._call("select", "select_option", {"entity_id": sel, "option": programme})
            if await self._wait_for(sel, lambda s: s == programme, 10) is None:
                return fail(f"Could not select programme '{programme}'" + hint)
        else:
            programme = (await self._state(sel)).get("state")
            if programme in BAD:
                return fail("No programme selected")

        # 5. Options (duration, temperature, ...) – wait until HA lets us set them
        wanted: dict[str, tuple[str, float]] = {}
        raw_opts: dict[str, tuple[str, Any]] = {}
        if params.get("duration_s") is not None:
            raw_opts["duration"] = ("seconds", params["duration_s"])
        for role, val in (params.get("options") or {}).items():
            raw_opts[role] = ("native", val)
        if raw_opts:
            step("Setting options…")
        nudged = False
        for role, (kind, val) in raw_opts.items():
            eid = ents.get(role)
            if not eid:
                return fail(f"No '{role}' entity configured for {name}")
            # "unknown" just means no value yet (entity is settable); only "unavailable" blocks.
            ready = lambda s: s not in (None, "unavailable")
            st = await self._wait_for(eid, ready, 8)
            if st is None and not nudged:
                # Still unavailable: force Home Connect to reload options by switching to another
                # programme and back.
                nudged = True
                step("Reloading programme options…")
                opts_list = (await self._state(sel)).get("attributes", {}).get("options") or []
                alt = next((o for o in opts_list if o != programme and "microwave" in o), None) \
                    or next((o for o in opts_list if o != programme), None)
                if alt:
                    await self._call("select", "select_option", {"entity_id": sel, "option": alt})
                    await self._wait_for(sel, lambda s: s == alt, 8)
                    await self._call("select", "select_option", {"entity_id": sel, "option": programme})
                    await self._wait_for(sel, lambda s: s == programme, 8)
                    st = await self._wait_for(eid, ready, 10)
            if st is None:
                log.info("%s still unavailable after waiting – trying set_value anyway", eid)
                st = await self._state(eid)
            v = float(val)
            if kind == "seconds":
                unit = (st.get("attributes") or {}).get("unit_of_measurement")
                v = {"s": v, "min": v / 60, "h": v / 3600}.get(unit, v)
            wanted[role] = (eid, v)

        async def apply_options() -> bool:
            """Set any option that differs from the wanted value. True if nothing needed changing."""
            clean = True
            for eid, v in wanted.values():
                st = await self._state(eid)
                try:
                    cur = float(st.get("state"))
                except (TypeError, ValueError):
                    cur = None
                if cur is None or abs(cur - v) > 0.01:
                    clean = False
                    try:
                        await self._call("number", "set_value", {"entity_id": eid, "value": v})
                    except ConnectionError as e:
                        log.warning("set_value %s failed: %s", eid, e)
            return clean

        if wanted:
            for _ in range(8):
                if await apply_options():
                    break
                await asyncio.sleep(1)
            else:
                cur = {}
                for r_, (e_, _v) in wanted.items():
                    cur[r_] = (await self._state(e_)).get("state")
                return fail(f"Could not set options (current values: {cur})" + hint)

        # 6. Start
        step("Starting…")
        await self._call("select", "select_option", {"entity_id": act, "option": programme})

        # 7. Verify it is running; Home Connect may reset options to defaults on start,
        #    so keep re-asserting them for a few seconds.
        op = ents.get("operation_state")
        if op:
            loop = asyncio.get_event_loop()
            end = loop.time() + 20
            started = False
            last = None
            step("Confirming it started…")
            while loop.time() < end:
                last = (await self._state(op)).get("state")
                if last in ("run", "delayedstart"):
                    started = True
                    break
                if last in ("error", "actionrequired"):
                    return fail(f"{name} reported '{last}'")
                await asyncio.sleep(1)
            if not started:
                return fail(f"{name} did not start (state: {last})" + hint)
            if wanted:
                for _ in range(4):
                    await apply_options()
                    await asyncio.sleep(1.5)
        return {"ok": True}

    async def command(self, cmd: str, params: dict) -> dict:
        loop = asyncio.get_event_loop()
        params = params or {}

        if cmd == "start_program":
            app_id = params.get("appliance")
            task = self._start_tasks.get(app_id)
            if task and not task.done():
                return {"ok": False, "error": "A start is already in progress"}
            self._set_status(app_id, "running", "Starting…")
            self._start_tasks[app_id] = asyncio.create_task(self._start_runner(params))
            return {"ok": True, "accepted": True}

        if cmd == "start_status":
            return {"ok": True, "status": self._start_status.get(params.get("appliance"))}

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
