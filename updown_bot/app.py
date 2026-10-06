"""The main loop: wires market data, strategy, execution and bookkeeping together."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from . import markets, positions, risk, strategy, wallet
from .config import Config
from .execution import Broker
from .feed import BinanceFeed
from .journal import Journal
from .markets import Market, MarketData
from .redeem import Redeemer
from .state import Position, StateStore
from .tui import AssetRow, Dashboard

log = logging.getLogger(__name__)

# Polymarket needs a few seconds after the close to publish the final price.
RESOLUTION_GRACE_SEC = 8
# Quotes are not worth fetching until a round is this close to an entry window.
QUOTE_LEAD_SEC = 10
# After a failed order, leave that market alone for a moment.
ORDER_RETRY_SEC = 3
HEARTBEAT_SEC = 15
BALANCE_REFRESH_SEC = 30
WATCHDOG_TIMEOUT_SEC = 180
WATCHDOG_POLL_SEC = 15


class Bot:
    def __init__(self, cfg: Config, dash: Dashboard):
        self.cfg = cfg
        self.dash = dash
        self.store = StateStore(cfg.data_dir / "state.json")
        self.state = self.store.load()
        self.journal = Journal(cfg.data_dir)
        self.data = MarketData()
        self.feed = BinanceFeed(list(cfg.assets), cfg.binance_ws_url)
        self.broker = Broker(cfg)
        self.redeemer = self._make_redeemer()

        self._running = True
        self._last_tick = time.time()
        self._window_ts = 0
        self._retry_at: dict[tuple, float] = {}
        self._last_heartbeat = 0.0
        self._last_balance_refresh = 0.0

        dash.mode = "DRY RUN" if cfg.dry_run else "LIVE"
        dash.settings = self._describe_settings()

    def run(self) -> None:
        log.info("starting in %s mode: %s", self.dash.mode, self.dash.settings)
        if self.state.trade_count or self.state.open_positions:
            log.info(
                "resuming: %d open, %d/%d won, session P&L $%+.2f",
                len(self.state.open_positions),
                self.state.win_count,
                self.state.trade_count,
                self.state.cumulative_pnl_usdc,
            )
        self.feed.start()
        threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()
        while self._running:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
                time.sleep(3)
                continue
            time.sleep(self.cfg.scan_interval_sec)
        self.store.save(self.state)
        self.feed.stop()
        log.info("stopped")

    def stop(self) -> None:
        self._running = False

    def tick(self) -> None:
        now = time.time()
        self._last_tick = now

        closed_day = risk.roll_day(self.state, now)
        if closed_day is not None:
            log.info("new trading day, the last one closed at $%+.2f", closed_day)

        self._manage_positions(now)
        self._scan_markets(now)
        self._audit_wallet(now)
        if self.redeemer is not None:
            self.redeemer.sweep()

        change = risk.update_cooldown(self.cfg, self.state, time.time())
        if change:
            log.warning(change)

        self._refresh_dashboard()
        self._heartbeat(now)
        self.store.save(self.state)

    # -- exits ---------------------------------------------------------

    def _manage_positions(self, now: float) -> None:
        for pos in list(self.state.open_positions):
            if now < pos.end_ts:
                self._check_exit(pos, now)
            elif now >= pos.end_ts + RESOLUTION_GRACE_SEC:
                self._resolve(pos)

    def _check_exit(self, pos: Position, now: float) -> None:
        cfg = self.cfg
        if not (cfg.stop_loss_enabled or cfg.take_profit_enabled):
            return
        retry_key = ("exit", pos.slug, pos.entry_ts)
        if now < self._retry_at.get(retry_key, 0):
            return
        bid = self.broker.quote(pos.token_id).bid
        reason = positions.exit_signal(cfg, pos, bid, now)
        if reason is None:
            return
        sold = self.broker.sell(pos.slug, pos.token_id, pos.shares)
        if sold is None:
            self._retry_at[retry_key] = now + ORDER_RETRY_SEC
            return
        pos.shares = sold
        pnl = positions.sale_pnl(pos, bid, cfg.taker_fee_rate)
        self._close(pos, reason, bid, pnl)

    def _resolve(self, pos: Position) -> None:
        close = self.data.close_price(pos.asset, pos.window_ts)
        strike = pos.strike
        if strike is None:
            strike = self.data.strike(pos.asset, pos.window_ts)
        if close is None or strike is None:
            return
        won = positions.is_winner(pos.side, strike, close)
        pnl = positions.resolution_pnl(pos, won, self.cfg.taker_fee_rate)
        self._close(pos, "WIN" if won else "LOSS", 1.0 if won else 0.0, pnl, close)
        if won and not pos.dry_run and self.redeemer is not None:
            self.redeemer.queue(pos.condition_id, pos.slug)

    def _close(
        self,
        pos: Position,
        reason: str,
        exit_price: float,
        pnl: float,
        close_price: Optional[float] = None,
    ) -> None:
        state = self.state
        positions.book_result(state, pos, pnl, time.time())
        log.info(
            "closed %s %s (%s): %.3f -> %.3f, P&L $%+.3f, session $%+.2f, %d/%d won",
            pos.side,
            pos.slug,
            reason,
            pos.entry_price,
            exit_price,
            pnl,
            state.cumulative_pnl_usdc,
            state.win_count,
            state.trade_count,
        )
        self.journal.write(
            "results",
            {
                "slug": pos.slug,
                "asset": pos.asset,
                "side": pos.side,
                "reason": reason,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "shares": pos.shares,
                "stake_usdc": pos.stake_usdc,
                "pnl_usdc": pnl,
                "strike": pos.strike,
                "close": close_price,
                "dry_run": pos.dry_run,
                "cumulative_pnl_usdc": state.cumulative_pnl_usdc,
            },
        )

    # -- entries -------------------------------------------------------

    def _scan_markets(self, now: float) -> None:
        cfg = self.cfg
        window_ts = markets.current_window(now)
        if window_ts != self._window_ts:
            self._window_ts = window_ts
            self._retry_at.clear()
        self.dash.window_end = window_ts + markets.ROUND_SEC
        quote_horizon = max(cfg.late_max_ttr_sec, cfg.early_max_ttr_sec) + QUOTE_LEAD_SEC

        live: list[tuple[Market, AssetRow, Optional[float], Optional[float]]] = []
        for asset in cfg.assets:
            market = self.data.market(asset, window_ts)
            if market is None:
                continue
            strike = self.data.strike(asset, window_ts)
            spot = self.feed.price(asset)
            move = strategy.drift(strike, spot)
            row = AssetRow(strike=strike, spot=spot, move=move, status="waiting")
            self.dash.assets[asset] = row
            if market.end_ts - now <= quote_horizon:
                live.append((market, row, strike, move))

        books = self.broker.quotes(m.token_for(side) for m, *_ in live for side in strategy.SIDES)
        for market, row, strike, move in live:
            quotes = {side: books[market.token_for(side)] for side in strategy.SIDES}
            row.up_ask = quotes[strategy.UP].ask
            row.down_ask = quotes[strategy.DOWN].ask

            held = self.state.held_on(market.slug)
            decision = strategy.decide(
                cfg,
                ttr=market.end_ts - time.time(),
                quotes=quotes,
                held=held,
                open_count=len(self.state.open_positions),
                move=move,
                in_cooldown=self.state.in_cooldown(now),
            )
            row.status = decision.reason
            if decision.enter:
                self._enter(market, decision, strike, move, len(held) + 1)

    def _enter(
        self,
        market: Market,
        decision: strategy.Decision,
        strike: Optional[float],
        move: Optional[float],
        entry_number: int,
    ) -> None:
        cfg = self.cfg
        now = time.time()
        retry_key = ("entry", market.slug)
        if now < self._retry_at.get(retry_key, 0):
            return

        stake = strategy.stake_for(
            cfg, market.asset, self.state.in_cooldown(now), self._wallet_balance()
        )
        if stake <= 0:
            return
        ttr = market.end_ts - now
        log.info(
            "%s entry #%d: %s %s at %.3f, %.1fs left, move %s",
            decision.phase,
            entry_number,
            decision.side,
            market.slug,
            decision.ask,
            ttr,
            f"{move:+.4%}" if move is not None else "n/a",
        )
        fill = self.broker.buy(
            market.slug,
            decision.side,
            market.token_for(decision.side),
            decision.ask,
            stake,
            deadline=market.end_ts - cfg.late_min_ttr_sec,
        )
        if fill is None:
            self._retry_at[retry_key] = time.time() + ORDER_RETRY_SEC
            return

        pos = Position(
            slug=market.slug,
            asset=market.asset,
            window_ts=market.window_ts,
            end_ts=market.end_ts,
            side=decision.side,
            token_id=market.token_for(decision.side),
            entry_price=fill.price,
            shares=fill.shares,
            stake_usdc=fill.usdc,
            entry_ts=time.time(),
            dry_run=cfg.dry_run,
            strike=strike,
            condition_id=market.condition_id,
        )
        self.state.open_positions.append(pos)
        self.state.last_position_event_ts = pos.entry_ts
        self.journal.write(
            "entries",
            {
                "slug": pos.slug,
                "asset": pos.asset,
                "side": pos.side,
                "phase": decision.phase,
                "entry_number": entry_number,
                "ask_at_signal": decision.ask,
                "entry_price": pos.entry_price,
                "shares": pos.shares,
                "stake_usdc": pos.stake_usdc,
                "strike": strike,
                "move": move,
                "ttr_sec": ttr,
                "dry_run": pos.dry_run,
            },
        )

    # -- wallet --------------------------------------------------------

    def _wallet_balance(self) -> Optional[float]:
        creds = self.cfg.credentials
        if self.cfg.dry_run or creds is None:
            return None
        return wallet.usdc_balance(creds.rpc_url, creds.funding_address)

    def _audit_wallet(self, now: float) -> None:
        if self.cfg.dry_run:
            return
        if now - self._last_balance_refresh >= BALANCE_REFRESH_SEC:
            self._last_balance_refresh = now
            balance = self._wallet_balance()
            if balance is not None:
                self.dash.wallet_balance = balance

        if not risk.audit_due(self.cfg, self.state, now):
            return
        self.state.last_wallet_check_ts = now
        balance = self._wallet_balance()
        if balance is None:
            return
        audit = risk.audit_wallet(self.state, balance)
        if audit is None:
            log.info("wallet baseline set at $%.4f", balance)
            return
        log.log(
            logging.INFO if audit.ok else logging.WARNING,
            "wallet audit: holds $%.4f, expected $%.4f, delta $%+.4f",
            audit.balance,
            audit.expected,
            audit.delta,
        )
        self.journal.write(
            "wallet_audit",
            {"balance": audit.balance, "expected": audit.expected, "delta": audit.delta},
        )

    def _make_redeemer(self) -> Optional[Redeemer]:
        cfg = self.cfg
        creds = cfg.credentials
        if cfg.dry_run or creds is None or not cfg.auto_redeem_enabled:
            return None
        if creds.signature_type != 2:
            log.warning("auto-redeem only supports proxy wallets (signature type 2), disabled")
            return None
        try:
            return Redeemer(
                rpc_url=creds.rpc_url,
                private_key=creds.private_key,
                safe_address=creds.funder_address,
                state_path=cfg.data_dir / "redeemer.json",
                dry=cfg.auto_redeem_dry,
                min_interval_sec=cfg.auto_redeem_interval_sec,
            )
        except Exception as exc:
            log.error("auto-redeem disabled, setup failed: %s", exc)
            return None

    # -- reporting -----------------------------------------------------

    def _describe_settings(self) -> str:
        cfg = self.cfg
        parts = [
            ", ".join(cfg.assets),
            f"stake ${cfg.stake_usdc:.2f} x{cfg.max_entries_per_window}",
            f"late {cfg.late_min_price:.2f}-{cfg.late_max_price:.2f}"
            f" at {cfg.late_min_ttr_sec}-{cfg.late_max_ttr_sec}s",
        ]
        if cfg.early_enabled:
            parts.append(
                f"early {cfg.early_min_price:.2f}-{cfg.early_max_price:.2f}"
                f" at {cfg.early_min_ttr_sec}-{cfg.early_max_ttr_sec}s"
                f" on {cfg.early_min_move_pct:.3%}"
            )
        parts.append(f"daily limit ${cfg.max_daily_loss_usdc:.2f}")
        return " | ".join(parts)

    def _refresh_dashboard(self) -> None:
        dash, state = self.dash, self.state
        dash.feed_connected = self.feed.connected
        dash.positions = list(state.open_positions)
        dash.session_pnl = state.cumulative_pnl_usdc
        dash.day_pnl = state.day_pnl_usdc
        dash.trades = state.trade_count
        dash.wins = state.win_count
        dash.cooldown_until = state.cooldown_until_ts
        if state.initial_wallet_usdc is not None:
            dash.wallet_expected = state.initial_wallet_usdc + state.cumulative_pnl_usdc
        if self.redeemer is not None:
            dash.pending_redemptions = self.redeemer.pending_count
            dash.last_redeem_tx = self.redeemer.last_tx_hash

    def _heartbeat(self, now: float) -> None:
        """One line every few seconds, so a quiet log still shows what the bot sees."""
        if now - self._last_heartbeat < HEARTBEAT_SEC:
            return
        self._last_heartbeat = now
        cells = []
        for asset, row in self.dash.assets.items():
            move = f"{row.move:+.3%}" if row.move is not None else "n/a"
            cells.append(f"{asset} {move} ({row.status})")
        left = (self.dash.window_end or now) - time.time()
        log.info("%.0fs left | %s", left, " | ".join(cells) or "no markets found")

    def _watchdog(self) -> None:
        """Kill the process if the loop hangs, so a supervisor can restart it."""
        while self._running:
            time.sleep(WATCHDOG_POLL_SEC)
            stalled = time.time() - self._last_tick
            if self._running and stalled > WATCHDOG_TIMEOUT_SEC:
                log.error("main loop stalled for %.0fs, exiting", stalled)
                logging.shutdown()
                os._exit(1)
