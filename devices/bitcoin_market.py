"""
Bitcoin market data plugin.

Fetches:
  - BTC price in SEK from CoinGecko (free, no key)
  - Network difficulty + hashrate from mempool.space (free, no key)

Calculates:
  - Daily BTC revenue per TH/s (terahash) — the industry standard unit
  - Converts to SEK/day for a given hashrate in GH/s

The profitability formula:
  daily_btc_per_th = (86400 / 600) * 3.125 / (network_hashrate_th / 1)
                   = blocks_per_day * block_reward / total_network_TH

  daily_sek_per_gh = daily_btc_per_th * btc_price_sek / 1000

  Note: block_reward is currently 3.125 BTC (post-2024 halving)
        next halving approximately 2028

config.json keys:
  poll_interval    Seconds between polls (default: 300 — don't hammer CoinGecko)
"""

import asyncio
import json
import urllib.request
import urllib.error
from devices.registry import BaseDevice

BLOCK_REWARD_BTC = 3.125   # post-April 2024 halving


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "HomeControl/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _fetch_market_data() -> dict:
    # BTC price in SEK — CoinGecko free tier
    price_data = _fetch_json(
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin&vs_currencies=sek,usd&include_24hr_change=true"
    )
    btc_sek = price_data["bitcoin"]["sek"]
    btc_usd = price_data["bitcoin"]["usd"]
    change_24h = price_data["bitcoin"].get("sek_24h_change", 0)

    # Network difficulty + hashrate — mempool.space
    diff_data  = _fetch_json("https://mempool.space/api/v1/difficulty-adjustment")
    # Current network hashrate in H/s from blockchain.info (mempool doesn't expose it directly)
    stats_data = _fetch_json("https://blockchain.info/stats?format=json")
    # hash_rate from blockchain.info is in GH/s
    network_gh = stats_data.get("hash_rate", 0)
    network_th = network_gh / 1000   # convert GH/s → TH/s

    # Profitability calculation
    # blocks per day = 86400s / 600s avg block time = 144
    blocks_per_day   = 86400 / 600
    daily_btc_per_th = (blocks_per_day * BLOCK_REWARD_BTC) / network_th if network_th else 0
    daily_sek_per_th = daily_btc_per_th * btc_sek
    daily_sek_per_gh = daily_sek_per_th / 1000

    return {
        "btc_price_sek":      round(btc_sek, 0),
        "btc_price_usd":      round(btc_usd, 0),
        "btc_change_24h_pct": round(change_24h, 2),
        "network_hashrate_th": round(network_th, 0),
        "block_reward_btc":   BLOCK_REWARD_BTC,
        "daily_btc_per_th":   round(daily_btc_per_th, 8),
        "daily_sek_per_th":   round(daily_sek_per_th, 2),
        "daily_sek_per_gh":   round(daily_sek_per_gh, 4),
        # Difficulty adjustment info
        "difficulty_change_pct":    round(diff_data.get("difficultyChange", 0), 2),
        "next_retarget_blocks":     diff_data.get("remainingBlocks"),
        "estimated_retarget_date":  diff_data.get("estimatedRetargetDate"),
        "progress_pct":             round(diff_data.get("progressPercent", 0), 1),
    }


class BitcoinMarket(BaseDevice):
    device_type = "bitcoin_market"

    def __init__(self, poll_interval: int = 300):
        super().__init__()
        self.poll_interval = poll_interval

    async def snapshot(self) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _fetch_market_data)
