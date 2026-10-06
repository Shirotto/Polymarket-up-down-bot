import json

from updown_bot.state import State, StateStore


def test_round_trip(tmp_path, make_position):
    store = StateStore(tmp_path / "state.json")
    state = State(open_positions=[make_position()], cumulative_pnl_usdc=1.5, trade_count=3)
    store.save(state)
    assert StateStore(tmp_path / "state.json").load() == state


def test_missing_file_gives_a_fresh_state(tmp_path):
    state = StateStore(tmp_path / "nope" / "state.json").load()
    assert state.open_positions == []
    assert state.trade_count == 0


def test_corrupt_file_gives_a_fresh_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert StateStore(path).load().trade_count == 0


def test_unknown_keys_are_ignored(tmp_path, make_position):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(State(open_positions=[make_position()], win_count=2))
    data = json.loads(path.read_text())
    data["from_the_future"] = 1
    data["open_positions"][0]["also_new"] = True
    path.write_text(json.dumps(data))
    loaded = StateStore(path).load()
    assert loaded.win_count == 2
    assert loaded.open_positions[0].slug == "btc-updown-5m-1000"


def test_save_skips_unchanged_state_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = State()
    store.save(state)
    first_write = path.stat().st_mtime_ns
    store.save(state)
    assert path.stat().st_mtime_ns == first_write
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_held_on_filters_by_market(make_position):
    btc = make_position()
    eth = make_position(slug="eth-updown-5m-1000", asset="ETH")
    state = State(open_positions=[btc, eth])
    assert state.held_on("eth-updown-5m-1000") == [eth]
