import pytest

from updown_bot.strategy import DOWN, UP, Quote, decide, drift, stake_for


def quotes(up_ask, down_ask):
    return {UP: Quote(up_ask - 0.01, up_ask), DOWN: Quote(down_ask - 0.01, down_ask)}


def run(cfg, **kwargs):
    args = dict(
        ttr=30, quotes=quotes(0.90, 0.11), held=[], open_count=0, move=None, in_cooldown=False
    )
    args.update(kwargs)
    return decide(cfg, **args)


def test_drift():
    assert drift(100.0, 101.0) == pytest.approx(0.01)
    assert drift(None, 101.0) is None
    assert drift(100.0, None) is None


# late phase


def test_late_buys_the_favourite_inside_the_band(make_config):
    d = run(make_config())
    assert (d.enter, d.side, d.ask, d.phase) == (True, UP, 0.90, "LATE")


def test_late_follows_whichever_side_is_favoured(make_config):
    d = run(make_config(), quotes=quotes(0.08, 0.93))
    assert d.side == DOWN


@pytest.mark.parametrize("up_ask", [0.84, 0.96])
def test_late_skips_favourite_outside_the_band(make_config, up_ask):
    assert not run(make_config(), quotes=quotes(up_ask, 0.10)).enter


@pytest.mark.parametrize("ttr, phase", [(2, "OFF"), (61, "WAIT")])
def test_late_respects_its_time_window(make_config, ttr, phase):
    d = run(make_config(early_enabled=False), ttr=ttr)
    assert not d.enter
    assert d.phase == phase


def test_high_price_is_not_paid_too_early(make_config):
    cfg = make_config()
    assert not run(cfg, ttr=50).enter
    assert run(cfg, ttr=45).enter


def test_tie_goes_to_up(make_config):
    cfg = make_config(late_min_price=0.40)
    assert run(cfg, quotes=quotes(0.50, 0.50)).side == UP


def test_one_sided_book_is_ignored(make_config):
    book = {UP: Quote(None, 0.90), DOWN: Quote(None, None)}
    d = run(make_config(), quotes=book)
    assert not d.enter
    assert d.reason == "no quotes"


# early phase


def test_early_buys_the_side_the_price_moved_to(make_config):
    d = run(make_config(), ttr=120, quotes=quotes(0.70, 0.31), move=0.001)
    assert (d.enter, d.side, d.phase) == (True, UP, "EARLY")
    d = run(make_config(), ttr=120, quotes=quotes(0.31, 0.70), move=-0.001)
    assert d.side == DOWN


def test_early_needs_a_big_enough_move(make_config):
    assert not run(make_config(), ttr=120, quotes=quotes(0.70, 0.31), move=0.0004).enter


@pytest.mark.parametrize("up_ask", [0.55, 0.88])
def test_early_price_band(make_config, up_ask):
    assert not run(make_config(), ttr=120, quotes=quotes(up_ask, 0.20), move=0.001).enter


def test_early_does_not_fall_back_to_the_favourite(make_config):
    # Inside the early window a weak move means wait, even if the book has
    # a favourite in the late band.
    cfg = make_config(late_max_ttr_sec=120, high_band_max_ttr_sec=120)
    assert not run(cfg, ttr=100, move=0.0001).enter


def test_without_a_spot_price_the_early_window_uses_late_rules(make_config):
    cfg = make_config(late_max_ttr_sec=120, high_band_max_ttr_sec=120)
    d = run(cfg, ttr=100, move=None)
    assert d.enter and d.phase == "LATE"


def test_cooldown_disables_early_and_narrows_late(make_config):
    cfg = make_config()
    assert not run(cfg, ttr=120, quotes=quotes(0.70, 0.31), move=0.001, in_cooldown=True).enter
    assert not run(cfg, ttr=30, in_cooldown=True).enter
    assert run(cfg, ttr=10, in_cooldown=True).enter


# limits and scaling


def test_global_position_limit(make_config):
    assert not run(make_config(max_open_positions=1), open_count=1).enter


def test_round_entry_limit(make_config, make_position):
    cfg = make_config(max_open_positions=5, max_entries_per_window=1)
    assert not run(cfg, held=[make_position()], open_count=1).enter


def test_round_stake_cap(make_config, make_position):
    cfg = make_config(max_open_positions=5, max_stake_per_window_usdc=7.0)
    assert not run(cfg, held=[make_position(stake_usdc=7.0)], open_count=1).enter


def test_scaling_needs_a_higher_price_than_the_last_entry(make_config, make_position):
    cfg = make_config(max_open_positions=5)
    held = [make_position(entry_price=0.88)]
    assert not run(cfg, held=held, open_count=1, quotes=quotes(0.90, 0.11)).enter
    d = run(cfg, held=held, open_count=1, quotes=quotes(0.91, 0.10))
    assert d.enter and d.phase == "SCALE"


def test_never_buys_against_a_held_side(make_config, make_position):
    cfg = make_config(max_open_positions=5)
    held = [make_position(side=UP, entry_price=0.60)]
    assert not run(cfg, held=held, open_count=1, quotes=quotes(0.08, 0.93)).enter
    early = run(cfg, held=held, open_count=1, ttr=120, quotes=quotes(0.31, 0.70), move=-0.0008)
    assert not early.enter


def test_early_scaling_stops_when_the_move_is_stretched(make_config, make_position):
    cfg = make_config(max_open_positions=5)
    held = [make_position(entry_price=0.62)]
    book = quotes(0.70, 0.31)
    assert run(cfg, held=held, open_count=1, ttr=120, quotes=book, move=0.0009).enter
    assert not run(cfg, held=held, open_count=1, ttr=120, quotes=book, move=0.0011).enter


# stake


def test_stake(make_config):
    cfg = make_config(stake_usdc=4.0, asset_stake_mult={"XRP": 0.5})
    assert stake_for(cfg, "BTC", False, None) == 4.0
    assert stake_for(cfg, "XRP", False, None) == 2.0
    assert stake_for(cfg, "BTC", True, None) == 2.0
    assert stake_for(cfg, "BTC", False, 3.0) == 2.7
    assert stake_for(cfg, "BTC", False, 100.0) == 4.0
