"""USDC balance lookup over plain JSON-RPC."""

from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

# USDC.e on Polygon, the collateral token of Polymarket's markets.
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_DECIMALS = 6
_BALANCE_OF = "0x70a08231"


def balance_of_calldata(address: str) -> str:
    return _BALANCE_OF + address.lower().removeprefix("0x").zfill(64)


def usdc_balance(rpc_url: str, address: str) -> Optional[float]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": USDC_ADDRESS, "data": balance_of_calldata(address)}, "finalized"],
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=8)
        resp.raise_for_status()
        return int(resp.json()["result"], 16) / 10**USDC_DECIMALS
    except (requests.RequestException, KeyError, ValueError) as exc:
        log.warning("USDC balance read failed: %s", exc)
        return None
