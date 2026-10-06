import pytest

from updown_bot import risk
from updown_bot.state import State


def test_roll_day():
    state = State(day_start_ts=0, day_pnl_usdc=-3.0)
    assert risk.roll_day(state, now=risk.DAY_SEC - 1) is None
    assert risk.roll_day(state, now=risk.DAY_SEC) == -3.0
    assert state.day_pnl_usdc == 0.0
    assert state.day_start_ts == risk.DAY_SEC


def test_daily_loss_starts_a_cooldown(make_config):
    cfg = make_config(max_daily_loss_usdc=10.0, cooldown_sec=3600)
    state = State(day_pnl_usdc=-10.0)
    assert "daily loss" in risk.update_cooldown(cfg, state, now=1000)
    assert state.cooldown_until_ts == 4600
    assert state.in_cooldown(4599)
    assert not state.in_cooldown(4600)


def test_loss_streak_starts_a_cooldown(make_config):
    cfg = make_config(loss_streak_count=2, loss_streak_window_sec=7200)
    state = State(recent_loss_ts=[100.0, 5000.0])
    assert "2 losses" in risk.update_cooldown(cfg, state, now=6000)


def test_old_losses_drop_out_of_the_streak(make_config):
    cfg = make_config(loss_streak_count=2, loss_streak_window_sec=7200)
    state = State(recent_loss_ts=[100.0, 9000.0])
    assert risk.update_cooldown(cfg, state, now=9500) is None
    assert state.recent_loss_ts == [9000.0]


def test_nothing_changes_during_a_cooldown(make_config):
    state = State(cooldown_until_ts=5000, day_pnl_usdc=-50.0)
    assert risk.update_cooldown(make_config(), state, now=4000) is None
    assert state.cooldown_until_ts == 5000


def test_cooldown_end_wipes_the_slate(make_config):
    state = State(cooldown_until_ts=5000, day_pnl_usdc=-50.0, recent_loss_ts=[4000.0, 4500.0])
    assert "over" in risk.update_cooldown(make_config(), state, now=5000)
    assert state.cooldown_until_ts == 0
    assert state.day_pnl_usdc == 0
    assert state.recent_loss_ts == []
    # and it does not immediately start another one
    assert risk.update_cooldown(make_config(), state, now=5001) is None


def test_audit_waits_for_a_quiet_wallet(make_config, make_position):
    cfg = make_config(wallet_check_interval_sec=900, wallet_check_idle_sec=300)
    assert risk.audit_due(cfg, State(), now=10_000)
    assert not risk.audit_due(cfg, State(open_positions=[make_position()]), now=10_000)
    assert not risk.audit_due(cfg, State(last_wallet_check_ts=9_500), now=10_000)
    assert not risk.audit_due(cfg, State(last_position_event_ts=9_800), now=10_000)


def test_first_audit_sets_the_baseline():
    state = State()
    assert risk.audit_wallet(state, 100.0) is None
    assert state.initial_wallet_usdc == 100.0


def test_audit_compares_wallet_with_tracked_pnl():
    state = State(initial_wallet_usdc=100.0, cumulative_pnl_usdc=-4.0)
    audit = risk.audit_wallet(state, 96.2)
    assert audit.expected == pytest.approx(96.0)
    assert audit.delta == pytest.approx(0.2)
    assert audit.ok
    assert not risk.audit_wallet(state, 95.0).ok
