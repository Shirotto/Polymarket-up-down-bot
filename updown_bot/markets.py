"""Market discovery and round prices from Polymarket's HTTP APIs."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

ROUND_SEC = 300
GAMMA_URL = "https://gamma-api.polymarket.com"
# Not a documented API: this is the endpoint the polymarket.com front end
# uses to show a round's opening and closing price.
ROUND_PRICE_URL = "https://polymarket.com/api/crypto/crypto-price"

_CACHE_TTL_SEC = 3.0
_KEEP_ROUNDS = 12


@dataclass(frozen=True)
class Market:
    slug: str
    asset: str
    window_ts: int
    end_ts: int
    up_token_id: str
    down_token_id: str
    condition_id: Optional[str]

    def token_for(self, side: str) -> str:
        return self.up_token_id if side == "UP" else self.down_token_id


def current_window(now: float) -> int:
    """Start timestamp of the round that contains ``now``."""
    return int(now) - int(now) % ROUND_SEC


def market_slug(asset: str, window_ts: int) -> str:
    return f"{asset.lower()}-updown-5m-{window_ts}"


def _as_list(value: Any) -> list:
    # Gamma returns these two fields as JSON-encoded strings.
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, list) else []


def parse_event(events: Any, asset: str, window_ts: int) -> Optional[Market]:
    """Build a Market from a Gamma ``/events`` response, or None if it doesn't fit."""
    try:
        market = events[0]["markets"][0]
        tokens = _as_list(market.get("clobTokenIds"))
        outcomes = [str(o).lower() for o in _as_list(market.get("outcomes"))]
        up, down = outcomes.index("up"), outcomes.index("down")
        condition_id = market.get("conditionId")
        return Market(
            slug=market_slug(asset, window_ts),
            asset=asset.upper(),
            window_ts=window_ts,
            end_ts=window_ts + ROUND_SEC,
            up_token_id=str(tokens[up]),
            down_token_id=str(tokens[down]),
            condition_id=str(condition_id) if condition_id else None,
        )
    except (LookupError, TypeError, ValueError, AttributeError):
        return None


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MarketData:
    """Short-lived cache in front of the two HTTP endpoints.

    The main loop asks for the same round many times a second; without the
    cache every tick would cost two requests per asset.
    """

    def __init__(self, session: Optional[requests.Session] = None):
        self._http = session or requests.Session()
        self._http.headers["User-Agent"] = "polymarket-updown-bot"
        self._markets: dict[tuple[str, int], tuple[float, Optional[Market]]] = {}
        self._prices: dict[tuple[str, int], tuple[float, dict]] = {}
        self._strikes: dict[tuple[str, int], float] = {}

    def market(self, asset: str, window_ts: int) -> Optional[Market]:
        key = (asset, window_ts)
        cached = self._markets.get(key)
        if cached and time.time() - cached[0] < _CACHE_TTL_SEC:
            return cached[1]
        found = None
        try:
            resp = self._http.get(
                f"{GAMMA_URL}/events",
                params={"slug": market_slug(asset, window_ts)},
                timeout=8,
            )
            resp.raise_for_status()
            found = parse_event(resp.json(), asset, window_ts)
        except (requests.RequestException, ValueError) as exc:
            log.debug("market lookup failed for %s: %s", market_slug(asset, window_ts), exc)
        self._markets[key] = (time.time(), found)
        self._prune(window_ts)
        return found

    def strike(self, asset: str, window_ts: int) -> Optional[float]:
        """Opening price of the round. It never changes once published."""
        key = (asset, window_ts)
        if key not in self._strikes:
            self._round_prices(asset, window_ts)
        return self._strikes.get(key)

    def close_price(self, asset: str, window_ts: int) -> Optional[float]:
        """Closing price of the round, None until Polymarket publishes it."""
        value = self._round_prices(asset, window_ts).get("closePrice")
        return float(value) if value is not None else None

    def _round_prices(self, asset: str, window_ts: int) -> dict:
        key = (asset, window_ts)
        cached = self._prices.get(key)
        if cached and time.time() - cached[0] < _CACHE_TTL_SEC:
            return cached[1]
        try:
            resp = self._http.get(
                ROUND_PRICE_URL,
                params={
                    "symbol": asset,
                    "eventStartTime": _iso(window_ts),
                    "variant": "fiveminute",
                    "endDate": _iso(window_ts + ROUND_SEC),
                },
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json() or {}
        except (requests.RequestException, ValueError) as exc:
            log.debug("round price lookup failed for %s %s: %s", asset, window_ts, exc)
            data = {}
        self._prices[key] = (time.time(), data)
        if data.get("openPrice") is not None:
            self._strikes[key] = float(data["openPrice"])
        return data

    def _prune(self, window_ts: int) -> None:
        cutoff = window_ts - _KEEP_ROUNDS * ROUND_SEC
        for cache in (self._markets, self._prices, self._strikes):
            for key in [k for k in cache if k[1] < cutoff]:
                del cache[key]
