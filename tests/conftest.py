import dataclasses

import pytest

from updown_bot.config import load_config
from updown_bot.state import Position


@pytest.fixture
def make_config():
    def build(**overrides):
        return dataclasses.replace(load_config({}), **overrides)

    return build


@pytest.fixture
def make_position():
    def build(**overrides):
        fields = dict(
            slug="btc-updown-5m-1000",
            asset="BTC",
            window_ts=1000,
            end_ts=1300,
            side="UP",
            token_id="111",
            entry_price=0.70,
            shares=10.0,
            stake_usdc=7.0,
            entry_ts=1100.0,
            dry_run=True,
            strike=50_000.0,
        )
        fields.update(overrides)
        return Position(**fields)

    return build
