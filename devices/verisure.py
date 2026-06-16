"""
Verisure alarm system device plugin.

Uses the unofficial python-verisure library (vsure) which talks to
the Verisure app GraphQL API.

FIRST-TIME SETUP (one-off, interactive):
  MFA is mandatory. Run this script once to generate the cookie:

    cat > /tmp/verisure_mfa.py << 'EOF'
    import verisure
    session = verisure.Session("your@email.com", "yourpassword", "verisure_cookie.pkl")
    session.request_mfa()
    code = input("Enter MFA code: ")
    session.validate_mfa(code)
    print("Cookie saved.")
    EOF
    python3 /tmp/verisure_mfa.py
    rm /tmp/verisure_mfa.py

  After that HomeControl runs headlessly — the cookie is refreshed automatically
  using the vs-refresh token (no password needed, no MFA prompts).

config.json keys (under "verisure" device):
  username        Verisure account email (used in GraphQL query, not for auth)
  cookie_file     Path to pickle cookie file (e.g. "verisure_cookie.pkl")
  installation    Installation index or alias string (default: 0)
  poll_interval   Seconds between polls (default: 60)
  door_labels     Optional dict mapping deviceLabel -> friendly name
                  e.g. {"3SJP H3ZZ": "Front door", "2CVH 8CMF": "Back door"}
"""

import asyncio
import logging
from typing import Optional
from devices.registry import BaseDevice

log = logging.getLogger("verisure")


def _load_verisure():
    try:
        import verisure
        return verisure
    except ImportError:
        raise ImportError(
            "The 'vsure' package is required.\n"
            "Install it with:  pip install vsure"
        )


def _fetch_all(username: str, cookie_file: str, installation, door_labels: dict) -> dict:
    """
    Synchronous Verisure data fetch. Runs in a thread executor.

    Flow:
      1. Load cookie from file
      2. Refresh vs-access token using vs-refresh (no password needed)
      3. Fetch arm state, climate sensors, door/window sensors
    """
    verisure = _load_verisure()

    if not username:
        raise ValueError(
            "Verisure requires 'username' (your email) in config.json.\n"
            "It is used as a GraphQL query variable, not for authentication."
        )

    session = verisure.Session(username, "", cookie_file)

    # Load cookie from disk
    try:
        session._load_cookie_file_into_memory()
    except Exception as e:
        raise ConnectionError(
            f"Could not read Verisure cookie '{cookie_file}': {e}\n"
            "Re-run the MFA setup script to regenerate it."
        )

    # Refresh the short-lived vs-access token using the long-lived vs-refresh token
    try:
        session.update_cookie()
    except Exception as e:
        raise ConnectionError(
            f"Verisure token refresh failed: {e}\n"
            "The session may have fully expired. Re-run the MFA setup script."
        )

    # Get installations
    try:
        resp = session.get_installations()
    except Exception as e:
        raise ConnectionError(f"Verisure get_installations failed: {e}")

    if "errors" in resp:
        raise ConnectionError(f"Verisure API error: {resp['errors']}")

    inst_list = resp.get("data", {}).get("account", {}).get("installations", [])
    if not inst_list:
        raise ValueError("No Verisure installations found")

    # Select installation by index or alias
    if isinstance(installation, str):
        match = next((i for i in inst_list if i.get("alias") == installation), None)
        if not match:
            available = [i.get("alias") for i in inst_list]
            raise ValueError(f"Installation '{installation}' not found. Available: {available}")
        session.set_giid(match["giid"])
    else:
        session.set_giid(inst_list[int(installation)]["giid"])

    result = {}

    # ── Alarm state ──────────────────────────────────────────────────────────
    try:
        r = session.request(session.arm_state())
        arm = r.get("data", {}).get("installation", {}).get("armState", {})
        result["alarm_state"]      = arm.get("statusType", "UNKNOWN")
        result["alarm_changed_by"] = arm.get("name", "")
        result["alarm_changed_at"] = arm.get("date", "")
        result["alarm_changed_via"] = arm.get("changedVia", "")
    except Exception as e:
        log.warning(f"Could not fetch arm state: {e}")
        result["alarm_state"] = "UNKNOWN"

    # ── Climate sensors ───────────────────────────────────────────────────────
    try:
        r = session.request(session.climate())
        climates = r.get("data", {}).get("installation", {}).get("climates", [])
        result["climate"] = [
            {
                "name":        c.get("device", {}).get("area", "Unknown"),
                "label":       c.get("device", {}).get("deviceLabel", ""),
                "type":        c.get("device", {}).get("gui", {}).get("label", ""),
                "temperature": c.get("temperatureValue"),
                "humidity":    c.get("humidityValue") if c.get("humidityEnabled") else None,
                "time":        c.get("temperatureTimestamp"),
            }
            for c in climates
        ]
    except Exception as e:
        log.warning(f"Could not fetch climate: {e}")
        result["climate"] = []

    # ── Door/window sensors ───────────────────────────────────────────────────
    # The library's door_window() query doesn't include device.area, so we
    # send a custom GraphQL query using session._post — this reuses the
    # library's URL-rotation logic (automation01 vs automation02).
    try:
        resp = session._post(
            url="/graphql",
            headers={"APPLICATION_ID": "PS_PYTHON", "Content-Type": "application/json"},
            cookies=session._cookies,
            json={
                "operationName": "Q",
                "variables": {"giid": session._giid},
                "query": (
                    "query Q($giid: String!) { installation(giid: $giid) { "
                    "doorWindows { device { deviceLabel area gui { label } } "
                    "state reportTime } } }"
                ),
            },
        )
        body = resp.json()
        if "errors" in body:
            raise ValueError(f"GraphQL errors: {body['errors']}")
        dws = (body.get("data") or {}).get("installation") or {}
        dws = dws.get("doorWindows") or []
        result["door_window"] = [
            {
                # Priority: config door_labels > API area > deviceLabel
                "name":  door_labels.get(
                             d.get("device", {}).get("deviceLabel", ""),
                             d.get("device", {}).get("area") or d.get("device", {}).get("deviceLabel", "Unknown")
                         ),
                "label": d.get("device", {}).get("deviceLabel", ""),
                "type":  d.get("device", {}).get("gui", {}).get("label", ""),
                "state": d.get("state", "UNKNOWN"),
                "time":  d.get("reportTime", ""),
            }
            for d in dws
        ]
    except Exception as e:
        log.warning(f"Could not fetch door/window: {e}")
        result["door_window"] = []

    # ── Smart plugs ──────────────────────────────────────────────────────────
    try:
        r = session.request(session.smartplugs())
        plugs = (r.get("data") or {}).get("installation", {}).get("smartplugs") or []
        result["smartplugs"] = [
            {
                "name":  door_labels.get(
                             p.get("device", {}).get("deviceLabel", ""),
                             p.get("device", {}).get("area") or p.get("device", {}).get("deviceLabel", "Unknown")
                         ),
                "label": p.get("device", {}).get("deviceLabel", ""),
                "state": p.get("currentState", "UNKNOWN"),  # ON / OFF
                "icon":  p.get("icon", ""),
            }
            for p in plugs
        ]
    except Exception as e:
        log.warning(f"Could not fetch smartplugs: {e}")
        result["smartplugs"] = []

    # ── Summary counts ────────────────────────────────────────────────────────
    result["open_doors"]    = sum(1 for d in result["door_window"] if d["state"] == "OPEN")
    result["climate_count"] = len(result["climate"])
    result["sensor_count"]  = len(result["door_window"])
    result["smartplug_count"] = len(result["smartplugs"])

    return result


def _set_smartplug(username: str, cookie_file: str, installation, device_label: str, state: bool) -> dict:
    """Turn a Verisure smart plug on (True) or off (False)."""
    verisure = _load_verisure()

    session = verisure.Session(username, "", cookie_file)
    session._load_cookie_file_into_memory()
    session.update_cookie()

    installations = session.get_installations()
    if "errors" in installations:
        raise ConnectionError(f"Verisure API error: {installations['errors']}")

    inst_list = installations.get("data", {}).get("account", {}).get("installations", [])
    if not inst_list:
        raise ValueError("No Verisure installations found")

    if isinstance(installation, str):
        match = next((i for i in inst_list if i.get("alias") == installation), None)
        if not match:
            raise ValueError(f"Installation '{installation}' not found")
        session.set_giid(match["giid"])
    else:
        session.set_giid(inst_list[int(installation)]["giid"])

    resp = session.request(session.set_smartplug(device_label, state))
    if "errors" in resp:
        raise ConnectionError(f"Failed to set smartplug: {resp['errors']}")
    return resp


class VerisureSystem(BaseDevice):
    device_type = "verisure"

    def __init__(
        self,
        username: str,
        cookie_file: str,
        installation: int = 0,
        poll_interval: int = 60,
        door_labels: Optional[dict] = None,
    ):
        super().__init__()
        self.username     = username
        self.cookie_file  = cookie_file
        self.installation = installation
        self.poll_interval = poll_interval
        self.door_labels  = door_labels or {}

    async def snapshot(self) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            _fetch_all,
            self.username,
            self.cookie_file,
            self.installation,
            self.door_labels,
        )

    async def command(self, cmd: str, params: dict) -> dict:
        """
        Supported commands:
          set_smartplug — params: {"device_label": "2AU6 CD4Z", "state": true/false}
                          or       {"name": "vardagsrum", "state": true/false}
                          (name is matched against the last known snapshot)
        """
        if cmd == "set_smartplug":
            device_label = params.get("device_label")
            state        = bool(params.get("state", False))

            # Allow lookup by friendly name from the last snapshot
            if not device_label:
                name = (params.get("name") or "").lower()
                plugs = self._last_snapshot.get("smartplugs", [])
                match = next((p for p in plugs if p.get("name", "").lower() == name), None)
                if not match:
                    raise ValueError(
                        f"Smart plug '{params.get('name')}' not found. "
                        f"Available: {[p.get('name') for p in plugs]}"
                    )
                device_label = match["label"]

            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                _set_smartplug,
                self.username,
                self.cookie_file,
                self.installation,
                device_label,
                state,
            )
            return {"ok": True, "device_label": device_label, "state": "ON" if state else "OFF", "raw": resp}

        raise NotImplementedError(f"Command '{cmd}' not yet implemented.")
