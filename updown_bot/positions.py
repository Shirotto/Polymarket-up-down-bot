"""Exit rules and P&L arithmetic for open positions."""

from __future__ import annotations

from typing import Optional

from .config import Config
from .state import Position, State
from .strategy import UP

STOP = "STOP"
TAKE_PROFIT = "TP"


def taker_fee(shares: float, price: float, rate: float) -> float:
    """Polymarket's taker fee curve: largest at 0.50, zero at the extremes."""
    return shares * rate * price * (1.0 - price)


def stop_price(cfg: Config, entry_price: float) -> float:
    if cfg.stop_loss_rel_pct > 0:
        return entry_price * (1.0 - cfg.stop_loss_rel_pct)
    return entry_price - cfg.stop_loss_delta


def exit_signal(cfg: Config, pos: Position, bid: Optional[float], now: float) -> Optional[str]:
    """Return STOP or TAKE_PROFIT if the position should be sold at ``bid``."""
    ttr = pos.end_ts - now
    if bid is None or ttr <= 0:
        return None
    # Close to the end the book thins out and a market sell gives away more
    # than simply waiting for resolution.
    if (
        cfg.stop_loss_enabled
        and ttr > cfg.stop_loss_min_ttr_sec
        and bid <= stop_price(cfg, pos.entry_price)
    ):
        return STOP
    if cfg.take_profit_enabled and bid >= cfg.take_profit_price:
        return TAKE_PROFIT
    return None


def sale_pnl(pos: Position, exit_price: float, fee_rate: float) -> float:
    fees = taker_fee(pos.shares, pos.entry_price, fee_rate)
    fees += taker_fee(pos.shares, exit_price, fee_rate)
    return pos.shares * exit_price - pos.stake_usdc - fees


def is_winner(side: str, strike: float, close: float) -> bool:
    """A round resolves UP when the close is at or above the strike."""
    return close >= strike if side == UP else close < strike


def resolution_pnl(pos: Position, won: bool, fee_rate: float) -> float:
    payout = pos.shares if won else 0.0
    return payout - pos.stake_usdc - taker_fee(pos.shares, pos.entry_price, fee_rate)


def book_result(state: State, pos: Position, pnl: float, now: float) -> None:
    """Remove a closed position and fold its result into the session counters."""
    state.open_positions = [p for p in state.open_positions if p is not pos]
    state.cumulative_pnl_usdc += pnl
    state.day_pnl_usdc += pnl
    state.trade_count += 1
    if pnl > 0:
        state.win_count += 1
    else:
        state.recent_loss_ts.append(now)
    state.last_position_event_ts = now
