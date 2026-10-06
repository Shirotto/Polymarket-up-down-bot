"""Terminal dashboard.

The bot loop writes into a ``Dashboard`` object and rich re-renders the
panels from it a few times a second, so the countdown keeps moving without
the loop having to drive the display. Without a TTY (for instance under
``docker compose up``) the dashboard is skipped and log lines go to stdout.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Optional

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .state import Position

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_DATEFMT = "%H:%M:%S"

_LEVEL_STYLE = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}
_DASH = Text("-", style="dim")


@dataclass
class AssetRow:
    strike: Optional[float] = None
    spot: Optional[float] = None
    move: Optional[float] = None
    up_ask: Optional[float] = None
    down_ask: Optional[float] = None
    status: str = ""


@dataclass
class Dashboard:
    mode: str = ""
    settings: str = ""
    feed_connected: bool = False
    window_end: Optional[int] = None
    assets: dict[str, AssetRow] = field(default_factory=dict)
    positions: list[Position] = field(default_factory=list)

    session_pnl: float = 0.0
    day_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    cooldown_until: float = 0.0

    wallet_balance: Optional[float] = None
    wallet_expected: Optional[float] = None
    pending_redemptions: int = 0
    last_redeem_tx: Optional[str] = None

    logs: deque = field(default_factory=lambda: deque(maxlen=60))


class _DashboardLogHandler(logging.Handler):
    def __init__(self, dash: Dashboard):
        super().__init__()
        self._dash = dash

    def emit(self, record: logging.LogRecord) -> None:
        stamp = time.strftime(LOG_DATEFMT, time.localtime(record.created))
        self._dash.logs.append((stamp, record.levelname, record.getMessage()))


class _Live:
    """Lets rich call a render function on every refresh."""

    def __init__(self, dash: Dashboard, render: Callable[[Dashboard], object]):
        self._dash = dash
        self._render = render

    def __rich__(self) -> object:
        return self._render(self._dash)


def _money(value: Optional[float], digits: int = 2) -> object:
    return f"${value:,.{digits}f}" if value is not None else _DASH


def _signed(value: float) -> Text:
    style = "green" if value > 0 else "red" if value < 0 else "white"
    return Text(f"${value:+.2f}", style=style)


def _header(d: Dashboard) -> Panel:
    win_rate = d.wins / d.trades if d.trades else 0.0
    top = Text.assemble(
        (d.mode, "bold magenta"),
        ("   feed ", "dim"),
        ("up", "green") if d.feed_connected else ("down", "yellow"),
        ("   session ", "dim"),
        _signed(d.session_pnl),
        ("   day ", "dim"),
        _signed(d.day_pnl),
        (f"   {d.wins}/{d.trades} won ({win_rate:.0%})", "dim"),
    )
    remaining = d.cooldown_until - time.time()
    if remaining > 0:
        top.append(f"   cooldown {remaining:.0f}s", style="bold cyan")
    return Panel(Group(top, Text(d.settings, style="dim")), border_style="cyan", padding=(0, 1))


def _assets(d: Dashboard) -> Panel:
    table = Table(box=None, expand=True, padding=(0, 1))
    table.add_column("asset", style="bold", no_wrap=True)
    table.add_column("spot", justify="right", no_wrap=True)
    table.add_column("strike", justify="right", no_wrap=True)
    table.add_column("move", justify="right", no_wrap=True)
    table.add_column("up", justify="right", no_wrap=True)
    table.add_column("down", justify="right", no_wrap=True)
    table.add_column("status", ratio=1, no_wrap=True, overflow="ellipsis")
    for asset in sorted(d.assets):
        row = d.assets[asset]
        if row.move is None:
            move: object = _DASH
        else:
            move = Text(f"{row.move:+.4%}", style="green" if row.move > 0 else "red")
        table.add_row(
            asset,
            _money(row.spot),
            _money(row.strike),
            move,
            _money(row.up_ask, 3),
            _money(row.down_ask, 3),
            Text(row.status, style="yellow" if row.status.startswith("buy") else "dim"),
        )
    return Panel(table, title="Markets", border_style="blue", padding=(0, 1))


def _positions(d: Dashboard) -> Panel:
    title = f"Open positions ({len(d.positions)})"
    if not d.positions:
        return Panel(Text("none", style="dim"), title=title, border_style="magenta")
    table = Table(box=None, expand=True, padding=(0, 1))
    for name in ("asset", "side"):
        table.add_column(name, no_wrap=True)
    for name in ("entry", "shares", "stake", "left"):
        table.add_column(name, justify="right", no_wrap=True)
    now = time.time()
    for p in sorted(d.positions, key=lambda p: (p.end_ts, p.entry_ts)):
        left = p.end_ts - now
        table.add_row(
            p.asset,
            Text(p.side, style="bold green" if p.side == "UP" else "bold red"),
            f"${p.entry_price:.3f}",
            f"{p.shares:.2f}",
            f"${p.stake_usdc:.2f}",
            f"{left:.0f}s" if left > 0 else "resolving",
        )
    return Panel(table, title=title, border_style="magenta", padding=(0, 1))


def _countdown(d: Dashboard) -> Panel:
    if d.window_end is None:
        body = Text("-", style="dim", justify="center")
    else:
        left = d.window_end - time.time()
        if left <= 0:
            body = Text("resolving", style="bold yellow", justify="center")
        elif left <= 60:
            style = "bold red" if left <= 30 else "bold yellow"
            body = Text(f"{left:05.2f}s", style=style, justify="center")
        else:
            minutes, seconds = divmod(int(left), 60)
            body = Text(f"{minutes:02d}:{seconds:02d}", style="bold green", justify="center")
    return Panel(body, title="Round ends in", border_style="yellow")


def _wallet(d: Dashboard) -> Panel:
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(justify="right", style="dim", no_wrap=True)
    grid.add_column()
    if d.wallet_balance is None:
        grid.add_row("balance", Text("not read (dry run)", style="dim"))
    else:
        grid.add_row("balance", _money(d.wallet_balance, 4))
        if d.wallet_expected is not None:
            delta = d.wallet_balance - d.wallet_expected
            grid.add_row("expected", _money(d.wallet_expected, 4))
            grid.add_row(
                "delta", Text(f"{delta:+.4f}", style="green" if abs(delta) < 0.5 else "red")
            )
    grid.add_row("to redeem", str(d.pending_redemptions))
    if d.last_redeem_tx:
        grid.add_row("last tx", d.last_redeem_tx[:12])
    return Panel(grid, title="Wallet", border_style="green")


class _LogTail:
    """The log panel, cut to the newest lines that fit its height."""

    def __init__(self, dash: Dashboard):
        self._dash = dash

    def __rich_console__(self, console, options):
        rows = max((options.height or options.max_height) - 2, 1)
        lines = [
            Text.assemble(
                (stamp + " ", "dim"), (f"{level:<7} ", _LEVEL_STYLE.get(level, "")), message
            )
            for stamp, level, message in list(self._dash.logs)[-rows:]
        ]
        yield Panel(Group(*lines), title="Log", border_style="white")


def _layout(dash: Dashboard) -> Layout:
    root = Layout()
    root.split_column(
        Layout(name="header", size=4),
        Layout(name="assets", size=10),
        Layout(name="middle", size=11),
        Layout(name="logs"),
    )
    root["middle"].split_row(Layout(name="positions", ratio=3), Layout(name="side", ratio=2))
    root["side"].split_column(Layout(name="countdown", size=3), Layout(name="wallet"))
    panels = {
        "header": _header,
        "assets": _assets,
        "positions": _positions,
        "countdown": _countdown,
        "wallet": _wallet,
    }
    for name, render in panels.items():
        root[name].update(_Live(dash, render))
    root["logs"].update(_LogTail(dash))
    return root


@contextlib.contextmanager
def display(dash: Dashboard, logger: logging.Logger, enabled: bool = True) -> Iterator[None]:
    """Show the dashboard for the duration of the block, or log to stdout."""
    if enabled and sys.stdout.isatty():
        handler: logging.Handler = _DashboardLogHandler(dash)
        logger.addHandler(handler)
        try:
            with Live(_layout(dash), refresh_per_second=8, screen=True):
                yield
        finally:
            logger.removeHandler(handler)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
