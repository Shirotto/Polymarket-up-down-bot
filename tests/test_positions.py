import pytest

from updown_bot import positions
from updown_bot.state import State


def test_stop_price_absolute_and_relative(make_config):
    assert positions.stop_price(make_config(stop_loss_delta=0.10), 0.70) == pytest.approx(0.60)
    assert positions.stop_price(make_config(stop_loss_rel_pct=0.20), 0.70) == pytest.approx(0.56)


def test_stop_fires_at_or_below_the_trigger(make_config, make_position):
    cfg, pos = make_config(), make_position(entry_price=0.70, end_ts=1300)
    assert positions.exit_signal(cfg, pos, 0.61, now=1200) is None
    assert positions.exit_signal(cfg, pos, 0.60, now=1200) == positions.STOP


def test_stop_is_off_near_the_end(make_config, make_position):
    cfg, pos = make_config(stop_loss_min_ttr_sec=10), make_position(end_ts=1300)
    assert positions.exit_signal(cfg, pos, 0.30, now=1291) is None


def test_stop_can_be_disabled(make_config, make_position):
    cfg = make_config(stop_loss_enabled=False)
    assert positions.exit_signal(cfg, make_position(), 0.10, now=1200) is None


def test_take_profit_is_opt_in(make_config, make_position):
    pos = make_position()
    assert positions.exit_signal(make_config(), pos, 0.99, now=1200) is None
    cfg = make_config(take_profit_enabled=True)
    assert positions.exit_signal(cfg, pos, 0.97, now=1200) is None
    assert positions.exit_signal(cfg, pos, 0.98, now=1200) == positions.TAKE_PROFIT


def test_no_exit_without_a_bid_or_after_the_close(make_config, make_position):
    cfg, pos = make_config(take_profit_enabled=True), make_position(end_ts=1300)
    assert positions.exit_signal(cfg, pos, None, now=1200) is None
    assert positions.exit_signal(cfg, pos, 0.99, now=1300) is None


@pytest.mark.parametrize(
    "side, close, won",
    [
        ("UP", 50_001, True),
        ("UP", 50_000, True),
        ("UP", 49_999, False),
        ("DOWN", 49_999, True),
        ("DOWN", 50_000, False),
    ],
)
def test_winner(side, close, won):
    assert positions.is_winner(side, 50_000, close) is won


def test_resolution_pnl(make_position):
    pos = make_position(shares=10.0, stake_usdc=7.0)
    assert positions.resolution_pnl(pos, True, 0.0) == pytest.approx(3.0)
    assert positions.resolution_pnl(pos, False, 0.0) == pytest.approx(-7.0)


def test_sale_pnl_with_fees(make_position):
    pos = make_position(shares=10.0, stake_usdc=7.0, entry_price=0.70)
    assert positions.sale_pnl(pos, 0.60, 0.0) == pytest.approx(-1.0)
    fees = 10 * 0.1 * 0.7 * 0.3 + 10 * 0.1 * 0.6 * 0.4
    assert positions.sale_pnl(pos, 0.60, 0.1) == pytest.approx(-1.0 - fees)


def test_book_result_updates_counters(make_position):
    winner, loser = make_position(), make_position(side="DOWN")
    state = State(open_positions=[winner, loser])
    positions.book_result(state, winner, 3.0, now=2000)
    positions.book_result(state, loser, -7.0, now=2001)
    assert state.open_positions == []
    assert (state.trade_count, state.win_count) == (2, 1)
    assert state.cumulative_pnl_usdc == pytest.approx(-4.0)
    assert state.recent_loss_ts == [2001]
    assert state.last_position_event_ts == 2001


def test_book_result_removes_only_that_position(make_position):
    first, second = make_position(), make_position()
    state = State(open_positions=[first, second])
    positions.book_result(state, first, 1.0, now=2000)
    assert state.open_positions == [second]
    assert state.open_positions[0] is second
