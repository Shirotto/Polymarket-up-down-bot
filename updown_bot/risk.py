"""Loss limits and the wallet audit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import Config
from .state import State

DAY_SEC = 86_400

# A gap between the wallet and the tracked P&L larger than this is flagged.
AUDIT_TOLERANCE_USDC = 0.5


def roll_day(state: State, now: float) -> Optional[float]:
    """Start a new 24h accounting period if the current one is over.

    Returns the P&L of the period that just ended, or None.
    """
    if now - state.day_start_ts < DAY_SEC:
        return None
    closed = state.day_pnl_usdc
    state.day_start_ts = now
    state.day_pnl_usdc = 0.0
    return closed


def recent_losses(state: State, window_sec: int, now: float) -> int:
    state.recent_loss_ts = [t for t in state.recent_loss_ts if t >= now - window_sec]
    return len(state.recent_loss_ts)


def update_cooldown(cfg: Config, state: State, now: float) -> Optional[str]:
    """Enter or leave the cooldown. Returns a message when something changed.

    During a cooldown the strategy halves its stake, drops early entries and
    only trades the last seconds of a round.
    """
    if state.cooldown_until_ts > now:
        return None
    if state.cooldown_until_ts > 0:
        state.cooldown_until_ts = 0.0
        state.recent_loss_ts.clear()
        state.day_pnl_usdc = 0.0
        return "cooldown over, back to normal size"

    if state.day_pnl_usdc <= -cfg.max_daily_loss_usdc:
        state.cooldown_until_ts = now + cfg.cooldown_sec
        return f"daily loss limit hit (${state.day_pnl_usdc:+.2f}), cooling down"
    losses = recent_losses(state, cfg.loss_streak_window_sec, now)
    if losses >= cfg.loss_streak_count:
        state.cooldown_until_ts = now + cfg.cooldown_sec
        return f"{losses} losses in {cfg.loss_streak_window_sec // 60} min, cooling down"
    return None


@dataclass(frozen=True)
class Audit:
    balance: float
    expected: float

    @property
    def delta(self) -> float:
        return self.balance - self.expected

    @property
    def ok(self) -> bool:
        return abs(self.delta) <= AUDIT_TOLERANCE_USDC


def audit_due(cfg: Config, state: State, now: float) -> bool:
    """The audit only makes sense when nothing is open and fills have settled."""
    if state.open_positions:
        return False
    if now - state.last_wallet_check_ts < cfg.wallet_check_interval_sec:
        return False
    return now - state.last_position_event_ts >= cfg.wallet_check_idle_sec


def audit_wallet(state: State, balance: float) -> Optional[Audit]:
    """Compare the wallet with what the tracked P&L says it should hold.

    The first reading becomes the baseline and yields no audit.
    """
    if state.initial_wallet_usdc is None:
        state.initial_wallet_usdc = balance
        return None
    return Audit(balance, state.initial_wallet_usdc + state.cumulative_pnl_usdc)
