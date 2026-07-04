"""
Antminer S9 with Braiins OS (BOSminer) device plugin.

Uses the CGMiner-compatible JSON-RPC API on TCP port 4028.
BOSminer response shape confirmed:
  {"STATUS":[...],"VERSION":[{"API":"3.7","BOSer":"boser-openwrt ..."}],"id":1}
  {"STATUS":[...],"SUMMARY":[{...}],"id":1}
  {"STATUS":[...],"POOLS":[{...}],"id":1}

Confirmed working commands (tested with nc):
  echo '{"command":"pause"}'   | nc <host> 4028
  echo '{"command":"resume"}'  | nc <host> 4028
  echo '{"command":"version"}' | nc <host> 4028
  echo '{"command":"summary"}' | nc <host> 4028

Note: BOSminer only wants {"command":"..."} — no "parameter" key for simple commands.
"""

import asyncio
import json
from devices.registry import BaseDevice


class S9Miner(BaseDevice):
    device_type = "s9_miner"

    def __init__(self, host: str, port: int = 4028, poll_interval: int = 15, power_w: int = 500):
        super().__init__()
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self.power_w = power_w

    async def _rpc(self, command: str, parameter: str = None) -> dict:
        """
        Send a BOSminer JSON-RPC command over TCP and return parsed response.

        Sends {"command": "..."} for simple commands, or
              {"command": "...", "parameter": "..."} when parameter is given.
        """
        msg = {"command": command}
        if parameter is not None:
            msg["parameter"] = parameter
        payload = json.dumps(msg).encode()

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=5
            )
            writer.write(payload)
            await writer.drain()

            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\x00" in chunk:   # BOSminer terminates responses with null byte
                    break

            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

            raw = b"".join(chunks).rstrip(b"\x00")
            return json.loads(raw)

        except (OSError, asyncio.TimeoutError) as e:
            raise ConnectionError(f"Cannot reach miner at {self.host}:{self.port} — {e}")

    async def is_mining(self) -> bool:
        """
        Return True if the miner is actively hashing.

        BOSminer does NOT zero out MHS 5s when paused — it keeps the last
        measured value. MHS 1m drops quickly when paused, so we compare it
        against MHS av (all-time average). If MHS 1m < 1% of MHS av, paused.
        """
        try:
            resp = await self._rpc("summary")
            summary = resp.get("SUMMARY", [{}])[0]
            mhs_1m = float(summary.get("MHS 1m", 0))
            mhs_av = float(summary.get("MHS av", 0))
            if mhs_av > 0:
                return (mhs_1m / mhs_av) > 0.01   # < 1% of av → effectively paused
            return float(summary.get("MHS 5s", 0)) > 0
        except Exception:
            return False

    async def snapshot(self) -> dict:
        """Fetch summary + pool stats. Returns a flat dict for the API."""

        summary_resp = await self._rpc("summary")
        summary = summary_resp.get("SUMMARY", [{}])[0]

        def gh(key_mhs, key_ghs):
            if key_mhs in summary:
                return round(summary[key_mhs] / 1000, 2)
            return round(summary.get(key_ghs, 0), 2)

        hashrate_5s  = gh("MHS 5s", "GHS 5s")
        hashrate_avg = gh("MHS av", "GHS av")

        pools_resp = await self._rpc("pools")
        pools = pools_resp.get("POOLS", [])
        active = next((p for p in pools if p.get("Stratum Active")), pools[0] if pools else {})

        temps = {}
        try:
            devs_resp = await self._rpc("devs")
            devs = devs_resp.get("DEVS", [])
            if devs:
                temp_vals = [d.get("Temperature", 0) for d in devs if d.get("Temperature")]
                if temp_vals:
                    temps["temp_avg_c"] = round(sum(temp_vals) / len(temp_vals), 1)
                    temps["temp_max_c"] = max(temp_vals)
        except Exception:
            pass

        return {
            "hashrate_5s_gh":    hashrate_5s,
            "hashrate_avg_gh":   hashrate_avg,
            "mining_active":     hashrate_5s > 0,
            "accepted_shares":   summary.get("Accepted", 0),
            "rejected_shares":   summary.get("Rejected", 0),
            "hw_errors":         summary.get("Hardware Errors", 0),
            "uptime_seconds":    summary.get("Elapsed", 0),
            "pool_url":          active.get("URL", ""),
            "pool_user":         active.get("User", ""),
            "pool_status":       active.get("Status", ""),
            "firmware":          "BOSminer",
            "power_w":           self.power_w,
            **temps,
        }

    async def command(self, cmd: str, params: dict) -> dict:
        if cmd == "pause":
            resp = await self._rpc("pause")
            return {"raw": resp}
        if cmd == "resume":
            resp = await self._rpc("resume")
            return {"raw": resp}
        if cmd == "restart":
            resp = await self._rpc("restart")
            return {"raw": resp}
        if cmd == "version":
            resp = await self._rpc("version")
            ver = resp.get("VERSION", [{}])[0]
            return {"api": ver.get("API"), "firmware": ver.get("BOSer", ver.get("CGMiner"))}
        raise NotImplementedError(f"Unknown command '{cmd}'")
