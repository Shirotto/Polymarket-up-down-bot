"""Live spot prices from Binance.

Polymarket only publishes a round's closing price after the round is over,
so there is nothing on their side to tell how far the price has moved while
the round is still open. Binance's ticker stream fills that gap. It is used
as a signal only: rounds are still settled on Polymarket's own price source.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import websocket

from .config import DEFAULT_BINANCE_WS_URL

log = logging.getLogger(__name__)

_MAX_BACKOFF_SEC = 30.0


def parse_tick(raw: str) -> Optional[tuple[str, float]]:
    """Extract ``(asset, last_price)`` from a combined-stream ticker message."""
    try:
        msg = json.loads(raw)
        pair, _, channel = msg["stream"].partition("@")
        if channel != "ticker" or not pair.endswith("usdt"):
            return None
        return pair[: -len("usdt")].upper(), float(msg["data"]["c"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class BinanceFeed:
    def __init__(self, assets: list[str], url: str = DEFAULT_BINANCE_WS_URL):
        self._assets = [a.upper() for a in assets]
        self._base_url = url
        self._prices: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="binance-feed")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def price(self, asset: str, max_age_sec: float = 5.0) -> Optional[float]:
        """Latest price, or None if the last tick is older than ``max_age_sec``."""
        with self._lock:
            entry = self._prices.get(asset.upper())
        if entry is None or time.time() - entry[1] > max_age_sec:
            return None
        return entry[0]

    def _url(self) -> str:
        streams = "/".join(f"{a.lower()}usdt@ticker" for a in self._assets)
        return f"{self._base_url}?streams={streams}"

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._listen()
                backoff = 1.0
            except Exception as exc:
                log.warning("Binance feed dropped (%s), reconnecting in %.0fs", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SEC)

    def _listen(self) -> None:
        ws = websocket.create_connection(self._url(), timeout=15)
        self._connected = True
        log.info("Binance feed connected (%s)", ", ".join(self._assets))
        try:
            while not self._stop.is_set():
                tick = parse_tick(ws.recv())
                if tick is None:
                    continue
                asset, price = tick
                with self._lock:
                    self._prices[asset] = (price, time.time())
        finally:
            self._connected = False
            ws.close()
