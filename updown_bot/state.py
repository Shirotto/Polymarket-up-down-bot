"""Positions and session counters, persisted so a restart picks up where it left off."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Position:
    slug: str
    asset: str
    window_ts: int
    end_ts: int
    side: str
    token_id: str
    entry_price: float
    shares: float
    stake_usdc: float
    entry_ts: float
    dry_run: bool
    strike: Optional[float] = None
    condition_id: Optional[str] = None


@dataclass
class State:
    open_positions: list[Position] = field(default_factory=list)
    initial_wallet_usdc: Optional[float] = None
    last_position_event_ts: float = 0.0
    last_wallet_check_ts: float = 0.0
    day_start_ts: float = field(default_factory=time.time)
    day_pnl_usdc: float = 0.0
    cumulative_pnl_usdc: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    recent_loss_ts: list[float] = field(default_factory=list)
    cooldown_until_ts: float = 0.0

    def in_cooldown(self, now: float) -> bool:
        return self.cooldown_until_ts > now

    def held_on(self, slug: str) -> list[Position]:
        return [p for p in self.open_positions if p.slug == slug]


_POSITION_FIELDS = {f.name for f in fields(Position)}
_STATE_FIELDS = {f.name for f in fields(State)} - {"open_positions"}


class StateStore:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._last_written: Optional[str] = None

    def load(self) -> State:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return State()
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("state file %s is unreadable (%s), starting fresh", self._path, exc)
            return State()

        # Unknown keys are dropped so an older or newer file still loads.
        positions = [
            Position(**{k: v for k, v in raw.items() if k in _POSITION_FIELDS})
            for raw in data.get("open_positions", [])
        ]
        scalars = {k: v for k, v in data.items() if k in _STATE_FIELDS}
        return State(open_positions=positions, **scalars)

    def save(self, state: State) -> None:
        payload = json.dumps(asdict(state), indent=2)
        if payload == self._last_written:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        tmp.write_text(payload)
        os.replace(tmp, self._path)
        self._last_written = payload
