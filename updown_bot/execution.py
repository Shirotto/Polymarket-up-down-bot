"""Quotes and orders on Polymarket's CLOB.

In dry-run mode the broker still reads live quotes, through the public
endpoints, but never signs or posts anything.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    BookParams,
    MarketOrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from .config import Config
from .strategy import Quote

log = logging.getLogger(__name__)

CLOB_URL = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# A fill-or-kill order for the full stake often dies on a thin book, so try
# half the size before giving up on the current price.
SIZE_LADDER = (1.0, 0.5)
MAX_CHASE_ROUNDS = 10
_TOKEN_UNITS = 1_000_000


@dataclass(frozen=True)
class Fill:
    shares: float
    usdc: float
    price: float


def best_quote(book: Any) -> Quote:
    """Top of an order book. Neither side is guaranteed to be sorted or present."""
    bids = [float(level.price) for level in book.bids or []]
    asks = [float(level.price) for level in book.asks or []]
    return Quote(bid=max(bids, default=None), ask=min(asks, default=None))


def _amount(resp: dict, key: str) -> Optional[float]:
    try:
        value = float(resp[key])
    except (KeyError, TypeError, ValueError):
        return None
    # The API is not consistent about units: the same field shows up either
    # as a decimal amount or in 6-decimal base units. No order this bot
    # places is anywhere near 1000 USDC, so a large number means base units.
    return value / _TOKEN_UNITS if value > 1000 else value


def parse_fill(resp: Any, stake: float, ref_price: float) -> Fill:
    """Work out what a buy order actually got from the order response.

    For a buy, ``makingAmount`` is the USDC paid and ``takingAmount`` the
    shares received. Whatever is missing is estimated from the stake and the
    price the order was aimed at.
    """
    resp = resp if isinstance(resp, dict) else {}
    usdc = _amount(resp, "makingAmount")
    shares = _amount(resp, "takingAmount")
    if usdc is None:
        usdc = stake
    if not shares:
        shares = usdc / ref_price
    return Fill(shares=shares, usdc=usdc, price=usdc / shares)


class Broker:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        creds = cfg.credentials
        if cfg.dry_run or creds is None:
            self._client = ClobClient(CLOB_URL)
            return
        self._client = ClobClient(
            CLOB_URL,
            key=creds.private_key,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=creds.signature_type,
            funder=creds.funder_address,
        )
        self._client.set_api_creds(
            ApiCreds(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                api_passphrase=creds.api_passphrase,
            )
        )

    def quotes(self, token_ids: Iterable[str]) -> dict[str, Quote]:
        """Best bid and ask for several tokens in a single request."""
        wanted = list(dict.fromkeys(token_ids))
        found: dict[str, Quote] = {}
        if wanted:
            try:
                books = self._client.get_order_books([BookParams(token_id=t) for t in wanted])
                found = {book.asset_id: best_quote(book) for book in books}
            except Exception as exc:
                log.debug("order book request failed: %s", exc)
        return {t: found.get(t, Quote(None, None)) for t in wanted}

    def quote(self, token_id: str) -> Quote:
        return self.quotes([token_id])[token_id]

    def share_balance(self, token_id: str) -> Optional[float]:
        try:
            resp = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
            )
            return float(resp["balance"]) / _TOKEN_UNITS
        except Exception as exc:
            log.debug("share balance read failed for %s: %s", token_id[:10], exc)
            return None

    def buy(
        self, slug: str, side: str, token_id: str, ask: float, stake: float, deadline: float
    ) -> Optional[Fill]:
        """Buy ``stake`` USDC worth of a token, following the ask until ``deadline``."""
        if self._cfg.dry_run:
            log.info("[dry] buy %s %s at %.3f for $%.2f", side, slug, ask, stake)
            return Fill(shares=stake / ask, usdc=stake, price=ask)

        fill = self._chase(slug, side, token_id, ask, stake, deadline)
        if fill is None:
            log.warning("buy %s %s abandoned", side, slug)
            return None
        if fill.price > self._cfg.max_chase_price:
            log.warning(
                "buy %s %s filled at %.4f, above the %.3f ceiling",
                side,
                slug,
                fill.price,
                self._cfg.max_chase_price,
            )
        return self._settled(fill, slug, token_id)

    def sell(self, slug: str, token_id: str, shares: float) -> Optional[float]:
        """Market-sell a position. Returns the shares sold, or None on failure."""
        if self._cfg.dry_run:
            log.info("[dry] sell %.4f shares of %s", shares, slug)
            return shares

        # Selling the nominal amount fails with "not enough balance" when the
        # wallet was credited slightly less than the fill reported.
        held = self.share_balance(token_id)
        amount = min(shares, held) if held else shares * 0.98
        amount = math.floor(amount * _TOKEN_UNITS) / _TOKEN_UNITS
        if amount <= 0:
            log.warning("nothing to sell on %s", slug)
            return None
        try:
            order = self._client.create_market_order(
                MarketOrderArgs(
                    token_id=token_id, amount=amount, side=SELL, order_type=OrderType.FOK
                )
            )
            self._client.post_order(order, OrderType.FOK)
        except Exception as exc:
            log.warning("sell failed on %s: %s", slug, exc)
            return None
        return amount

    def _chase(self, slug, side, token_id, ask, stake, deadline) -> Optional[Fill]:
        for _ in range(MAX_CHASE_ROUNDS):
            if time.time() >= deadline:
                log.info("buy %s %s: out of time", side, slug)
                return None
            if ask > self._cfg.max_chase_price:
                log.info("buy %s %s: ask %.3f is above the ceiling", side, slug, ask)
                return None
            fill = self._fill_or_kill(slug, side, token_id, ask, stake)
            if fill is not None:
                return fill
            new_ask = self.quote(token_id).ask
            if new_ask is None:
                return None
            if new_ask != ask:
                log.info("buy %s %s: ask moved %.3f -> %.3f", side, slug, ask, new_ask)
            ask = new_ask
        return None

    def _fill_or_kill(self, slug, side, token_id, ask, stake) -> Optional[Fill]:
        for fraction in SIZE_LADDER:
            amount = round(stake * fraction, 2)
            try:
                order = self._client.create_market_order(
                    MarketOrderArgs(
                        token_id=token_id, amount=amount, side=BUY, order_type=OrderType.FOK
                    )
                )
                resp = self._client.post_order(order, OrderType.FOK)
            except Exception as exc:
                log.debug("FOK buy of $%.2f on %s rejected: %s", amount, slug, exc)
                continue
            fill = parse_fill(resp, amount, ask)
            log.info(
                "bought %s %s: $%.2f for %.4f shares at %.4f (ask was %.3f)",
                side,
                slug,
                fill.usdc,
                fill.shares,
                fill.price,
                ask,
            )
            return fill
        return None

    def _settled(self, fill: Fill, slug: str, token_id: str) -> Fill:
        # The wallet tends to be credited 1-2% fewer shares than the order
        # response reports. Trusting the response would overstate every win.
        held = self.share_balance(token_id)
        if not held or held >= fill.shares:
            return fill
        if held < fill.shares * 0.99:
            log.info("%s: order reported %.4f shares, wallet holds %.4f", slug, fill.shares, held)
        return Fill(shares=held, usdc=fill.usdc, price=fill.price)
