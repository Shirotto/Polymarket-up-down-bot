"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Credentials:
    private_key: str
    api_key: str
    api_secret: str
    api_passphrase: str
    signature_type: int
    funder_address: str
    wallet_address: str
    rpc_url: str

    @property
    def funding_address(self) -> str:
        # Proxy wallets (signature type 2) hold the USDC on the funder
        # address; the signer key only authorises orders.
        return self.funder_address if self.signature_type == 2 else self.wallet_address


@dataclass(frozen=True)
class Config:
    assets: tuple[str, ...]
    dry_run: bool
    data_dir: Path
    scan_interval_sec: float
    binance_ws_url: str

    stake_usdc: float
    asset_stake_mult: dict[str, float]
    max_open_positions: int
    taker_fee_rate: float

    late_min_price: float
    late_max_price: float
    late_min_ttr_sec: int
    late_max_ttr_sec: int

    early_enabled: bool
    early_min_price: float
    early_max_price: float
    early_min_ttr_sec: int
    early_max_ttr_sec: int
    early_min_move_pct: float

    max_entries_per_window: int
    max_stake_per_window_usdc: float
    scale_price_step: float
    scale_max_move_pct: float

    high_band_price: float
    high_band_max_ttr_sec: int
    max_chase_price: float

    take_profit_enabled: bool
    take_profit_price: float
    stop_loss_enabled: bool
    stop_loss_delta: float
    stop_loss_rel_pct: float
    stop_loss_min_ttr_sec: int

    max_daily_loss_usdc: float
    loss_streak_window_sec: int
    loss_streak_count: int
    cooldown_sec: int

    wallet_check_interval_sec: int
    wallet_check_idle_sec: int

    auto_redeem_enabled: bool
    auto_redeem_dry: bool
    auto_redeem_interval_sec: int

    credentials: Optional[Credentials]


# Binance's market-data-only endpoint. Unlike stream.binance.com it is not
# tied to the trading API and is reachable from more networks.
DEFAULT_BINANCE_WS_URL = "wss://data-stream.binance.vision/stream"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

_CREDENTIAL_VARS = (
    "POLY_PRIVATE_KEY",
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_API_PASSPHRASE",
    "POLY_FUNDER_ADDRESS",
    "WALLET_ADDRESS",
    "POLYGON_RPC_URL",
)


class _Reader:
    def __init__(self, env: Mapping[str, str]):
        self._env = env

    def text(self, name: str, default: str = "") -> str:
        value = self._env.get(name)
        return value.strip() if value and value.strip() else default

    def number(self, name: str, default: float) -> float:
        raw = self.text(name)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from None

    def integer(self, name: str, default: int) -> int:
        raw = self.text(name)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from None

    def flag(self, name: str, default: bool) -> bool:
        raw = self.text(name).lower()
        if not raw:
            return default
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
        raise ConfigError(f"{name} must be true or false, got {raw!r}")


def parse_multipliers(raw: str) -> dict[str, float]:
    """Parse ``"XRP:0.5,ETH:0.75"`` into ``{"XRP": 0.5, "ETH": 0.75}``."""
    out: dict[str, float] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        asset, sep, value = part.partition(":")
        try:
            if not sep:
                raise ValueError
            out[asset.strip().upper()] = float(value)
        except ValueError:
            raise ConfigError(f"ASSET_STAKE_MULT: cannot parse {part!r}") from None
    return out


def _load_credentials(r: _Reader) -> Credentials:
    missing = [name for name in _CREDENTIAL_VARS if not r.text(name)]
    if missing:
        raise ConfigError(
            "live trading (DRY_RUN=false) needs these variables: " + ", ".join(missing)
        )
    return Credentials(
        private_key=r.text("POLY_PRIVATE_KEY"),
        api_key=r.text("POLY_API_KEY"),
        api_secret=r.text("POLY_API_SECRET"),
        api_passphrase=r.text("POLY_API_PASSPHRASE"),
        signature_type=r.integer("POLY_SIGNATURE_TYPE", 2),
        funder_address=r.text("POLY_FUNDER_ADDRESS"),
        wallet_address=r.text("WALLET_ADDRESS"),
        rpc_url=r.text("POLYGON_RPC_URL"),
    )


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    r = _Reader(os.environ if env is None else env)
    dry_run = r.flag("DRY_RUN", True)
    assets = tuple(
        a.strip().upper() for a in r.text("ASSETS", "BTC,ETH,SOL,XRP,DOGE").split(",") if a.strip()
    )
    cfg = Config(
        assets=assets,
        dry_run=dry_run,
        data_dir=Path(r.text("DATA_DIR", "data")),
        scan_interval_sec=r.number("SCAN_INTERVAL_SEC", 0.25),
        binance_ws_url=r.text("BINANCE_WS_URL", DEFAULT_BINANCE_WS_URL),
        stake_usdc=r.number("STAKE_USDC", 5.0),
        asset_stake_mult=parse_multipliers(r.text("ASSET_STAKE_MULT")),
        max_open_positions=r.integer("MAX_OPEN_POSITIONS", 1),
        taker_fee_rate=r.number("TAKER_FEE_RATE", 0.0),
        late_min_price=r.number("LATE_MIN_PRICE", 0.85),
        late_max_price=r.number("LATE_MAX_PRICE", 0.95),
        late_min_ttr_sec=r.integer("LATE_MIN_TTR_SEC", 3),
        late_max_ttr_sec=r.integer("LATE_MAX_TTR_SEC", 60),
        early_enabled=r.flag("EARLY_ENABLED", True),
        early_min_price=r.number("EARLY_MIN_PRICE", 0.60),
        early_max_price=r.number("EARLY_MAX_PRICE", 0.85),
        early_min_ttr_sec=r.integer("EARLY_MIN_TTR_SEC", 60),
        early_max_ttr_sec=r.integer("EARLY_MAX_TTR_SEC", 240),
        early_min_move_pct=r.number("EARLY_MIN_MOVE_PCT", 0.0005),
        max_entries_per_window=r.integer("MAX_ENTRIES_PER_WINDOW", 3),
        max_stake_per_window_usdc=r.number("MAX_STAKE_PER_WINDOW_USDC", 9.0),
        scale_price_step=r.number("SCALE_PRICE_STEP", 0.03),
        scale_max_move_pct=r.number("SCALE_MAX_MOVE_PCT", 0.001),
        high_band_price=r.number("HIGH_BAND_PRICE", 0.85),
        high_band_max_ttr_sec=r.integer("HIGH_BAND_MAX_TTR_SEC", 45),
        max_chase_price=r.number("MAX_CHASE_PRICE", 0.95),
        take_profit_enabled=r.flag("TAKE_PROFIT_ENABLED", False),
        take_profit_price=r.number("TAKE_PROFIT_PRICE", 0.98),
        stop_loss_enabled=r.flag("STOP_LOSS_ENABLED", True),
        stop_loss_delta=r.number("STOP_LOSS_DELTA", 0.10),
        stop_loss_rel_pct=r.number("STOP_LOSS_REL_PCT", 0.0),
        stop_loss_min_ttr_sec=r.integer("STOP_LOSS_MIN_TTR_SEC", 10),
        max_daily_loss_usdc=r.number("MAX_DAILY_LOSS_USDC", 10.0),
        loss_streak_window_sec=r.integer("LOSS_STREAK_WINDOW_SEC", 7200),
        loss_streak_count=r.integer("LOSS_STREAK_COUNT", 2),
        cooldown_sec=r.integer("COOLDOWN_SEC", 3600),
        wallet_check_interval_sec=r.integer("WALLET_CHECK_INTERVAL_SEC", 900),
        wallet_check_idle_sec=r.integer("WALLET_CHECK_IDLE_SEC", 300),
        auto_redeem_enabled=r.flag("AUTO_REDEEM_ENABLED", False),
        auto_redeem_dry=r.flag("AUTO_REDEEM_DRY", True),
        auto_redeem_interval_sec=r.integer("AUTO_REDEEM_INTERVAL_SEC", 90),
        credentials=None if dry_run else _load_credentials(r),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if not cfg.assets:
        raise ConfigError("ASSETS is empty")
    if cfg.stake_usdc <= 0:
        raise ConfigError("STAKE_USDC must be positive")
    if cfg.scan_interval_sec <= 0:
        raise ConfigError("SCAN_INTERVAL_SEC must be positive")
    if cfg.max_open_positions < 1 or cfg.max_entries_per_window < 1:
        raise ConfigError("MAX_OPEN_POSITIONS and MAX_ENTRIES_PER_WINDOW must be at least 1")

    bands = (
        ("LATE", cfg.late_min_price, cfg.late_max_price),
        ("EARLY", cfg.early_min_price, cfg.early_max_price),
    )
    for name, low, high in bands:
        if not 0 < low <= high < 1:
            raise ConfigError(
                f"{name}_MIN_PRICE and {name}_MAX_PRICE must satisfy 0 < min <= max < 1"
            )
    windows = (
        ("LATE", cfg.late_min_ttr_sec, cfg.late_max_ttr_sec),
        ("EARLY", cfg.early_min_ttr_sec, cfg.early_max_ttr_sec),
    )
    for name, low, high in windows:
        if not 0 <= low <= high:
            raise ConfigError(f"{name}_MIN_TTR_SEC must not exceed {name}_MAX_TTR_SEC")

    for name, value in (
        ("MAX_CHASE_PRICE", cfg.max_chase_price),
        ("TAKE_PROFIT_PRICE", cfg.take_profit_price),
        ("HIGH_BAND_PRICE", cfg.high_band_price),
    ):
        if not 0 < value <= 1:
            raise ConfigError(f"{name} must be in (0, 1]")
    if not 0 <= cfg.stop_loss_rel_pct < 1:
        raise ConfigError("STOP_LOSS_REL_PCT must be in [0, 1)")
