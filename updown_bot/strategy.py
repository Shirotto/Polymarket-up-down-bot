"""Entry rules. Everything here is a pure function of its arguments.

A round can be entered in two phases:

* early - a few minutes before the close, on the side the spot price has
  already moved towards, while that side is still relatively cheap;
* late  - in the last seconds, on whichever side the book favours, if its
  price sits inside a narrow band.

Additional entries on the same round ("scaling") are only allowed on the side
already held and only at a higher price than the previous entry, so the bot
never averages down.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .state import Position

UP = "UP"
DOWN = "DOWN"
SIDES = (UP, DOWN)

# While cooling down after losses only the last few seconds are tradable.
COOLDOWN_LATE_TTR_SEC = (2, 15)

# Never commit more than this share of the wallet to one order.
MAX_BALANCE_FRACTION = 0.9


@dataclass(frozen=True)
class Quote:
    bid: Optional[float]
    ask: Optional[float]


@dataclass(frozen=True)
class Decision:
    phase: str
    reason: str
    side: Optional[str] = None
    ask: Optional[float] = None

    @property
    def enter(self) -> bool:
        return self.side is not None


def drift(strike: Optional[float], spot: Optional[float]) -> Optional[float]:
    """Relative move of the spot price away from the round's strike."""
    if not strike or not spot:
        return None
    return (spot - strike) / strike


def decide(
    cfg: Config,
    *,
    ttr: float,
    quotes: Mapping[str, Quote],
    held: Sequence[Position],
    open_count: int,
    move: Optional[float],
    in_cooldown: bool,
) -> Decision:
    """Decide whether to buy on one market right now.

    ``held`` are the positions already open on this market, ``open_count``
    the positions open across all markets, ``ttr`` the seconds left before
    the round closes.
    """
    if open_count >= cfg.max_open_positions:
        return Decision("OFF", f"{open_count} open (max {cfg.max_open_positions})")
    if len(held) >= cfg.max_entries_per_window:
        return Decision("OFF", f"round full ({len(held)}/{cfg.max_entries_per_window})")
    staked = sum(p.stake_usdc for p in held)
    if staked >= cfg.max_stake_per_window_usdc:
        return Decision("OFF", f"round stake cap reached (${staked:.2f})")

    asks = {
        side: quotes[side].ask
        for side in SIDES
        if side in quotes and quotes[side].bid is not None and quotes[side].ask is not None
    }
    if not asks:
        return Decision("WAIT", "no quotes")

    in_early_window = cfg.early_min_ttr_sec <= ttr <= cfg.early_max_ttr_sec
    if cfg.early_enabled and not in_cooldown and move is not None and in_early_window:
        return _early(cfg, ttr, asks, held, move)
    return _late(cfg, ttr, asks, held, in_cooldown)


def _early(cfg, ttr, asks, held, move) -> Decision:
    if abs(move) < cfg.early_min_move_pct:
        return Decision("EARLY", f"move {move:+.4%} below {cfg.early_min_move_pct:.3%}")
    if held and abs(move) > cfg.scale_max_move_pct:
        return Decision("EARLY", f"move {move:+.4%} too stretched to add")

    side = UP if move > 0 else DOWN
    if held and side != held[0].side:
        return Decision("OFF", f"holding {held[0].side}, signal says {side}")
    ask = asks.get(side)
    if ask is None:
        return Decision("EARLY", f"no quote for {side}")
    if ask < cfg.early_min_price:
        return Decision("EARLY", f"{side} at {ask:.3f}, below {cfg.early_min_price:.2f}")
    if ask > cfg.early_max_price:
        return Decision("EARLY", f"{side} at {ask:.3f}, above {cfg.early_max_price:.2f}")
    return _confirm(cfg, "EARLY", side, ask, ttr, held)


def _late(cfg, ttr, asks, held, in_cooldown) -> Decision:
    if in_cooldown:
        min_ttr, max_ttr = COOLDOWN_LATE_TTR_SEC
    else:
        min_ttr, max_ttr = cfg.late_min_ttr_sec, cfg.late_max_ttr_sec
    if ttr < min_ttr:
        return Decision("OFF", f"too late ({ttr:.1f}s left)")
    if ttr > max_ttr:
        return Decision("WAIT", f"{ttr:.0f}s left")

    # max() keeps the first of equal elements, so a tie goes to UP.
    side = max(asks, key=asks.get)
    ask = asks[side]
    if ask < cfg.late_min_price:
        return Decision("LATE", f"favourite {side} at {ask:.3f}, below {cfg.late_min_price:.2f}")
    if ask > cfg.late_max_price:
        return Decision("LATE", f"favourite {side} at {ask:.3f}, above {cfg.late_max_price:.2f}")
    if held and side != held[0].side:
        return Decision("OFF", f"favourite flipped to {side}, holding {held[0].side}")
    return _confirm(cfg, "LATE", side, ask, ttr, held)


def _confirm(cfg, phase, side, ask, ttr, held) -> Decision:
    if held:
        last = max(held, key=lambda p: p.entry_ts)
        if ask < last.entry_price + cfg.scale_price_step:
            return Decision(
                phase,
                f"no follow-through: {ask:.3f} vs last entry {last.entry_price:.3f}",
            )
    # Paying a high price with a lot of time left leaves little upside and
    # the whole stake exposed to a reversal.
    if ask >= cfg.high_band_price and ttr > cfg.high_band_max_ttr_sec:
        return Decision(phase, f"{side} at {ask:.3f} with {ttr:.0f}s left, too early")
    return Decision("SCALE" if held else phase, f"buy {side} at {ask:.3f}", side=side, ask=ask)


def stake_for(cfg: Config, asset: str, in_cooldown: bool, balance: Optional[float]) -> float:
    stake = cfg.stake_usdc * cfg.asset_stake_mult.get(asset, 1.0)
    if in_cooldown:
        stake /= 2
    if balance is not None and balance > 0:
        stake = min(stake, balance * MAX_BALANCE_FRACTION)
    return round(stake, 2)
