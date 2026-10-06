import json

from updown_bot.markets import MarketData, current_window, market_slug, parse_event


def event(outcomes=("Up", "Down"), tokens=("111", "222"), encode=True, **extra):
    wrap = json.dumps if encode else list
    market = {
        "clobTokenIds": wrap(list(tokens)),
        "outcomes": wrap(list(outcomes)),
        "conditionId": "0xabc",
        **extra,
    }
    return [{"markets": [market]}]


def test_current_window():
    assert current_window(1_700_000_123.9) == 1_700_000_100
    assert current_window(1_700_000_100) == 1_700_000_100


def test_slug():
    assert market_slug("BTC", 1_700_000_100) == "btc-updown-5m-1700000100"


def test_parse_event():
    market = parse_event(event(), "btc", 1000)
    assert market.slug == "btc-updown-5m-1000"
    assert market.asset == "BTC"
    assert market.end_ts == 1300
    assert (market.up_token_id, market.down_token_id) == ("111", "222")
    assert market.condition_id == "0xabc"
    assert market.token_for("DOWN") == "222"


def test_parse_event_maps_tokens_by_outcome_name_not_position():
    market = parse_event(event(outcomes=("Down", "Up")), "BTC", 1000)
    assert (market.up_token_id, market.down_token_id) == ("222", "111")


def test_parse_event_accepts_plain_lists():
    assert parse_event(event(encode=False), "BTC", 1000).up_token_id == "111"


def test_parse_event_rejects_anything_else():
    assert parse_event([], "BTC", 1000) is None
    assert parse_event(None, "BTC", 1000) is None
    assert parse_event([{"markets": []}], "BTC", 1000) is None
    assert parse_event(event(outcomes=("Yes", "No")), "BTC", 1000) is None
    assert parse_event(event(tokens=("111",)), "BTC", 1000) is None
    assert parse_event([{"markets": [{"clobTokenIds": "not json"}]}], "BTC", 1000) is None


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.headers = {}
        self.payload = payload
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return FakeResponse(self.payload)


def test_strike_is_fetched_once_and_close_appears_later():
    session = FakeSession({"openPrice": 50_000.5, "closePrice": None})
    data = MarketData(session)
    assert data.strike("BTC", 1000) == 50_000.5
    assert data.close_price("BTC", 1000) is None
    assert data.strike("BTC", 1000) == 50_000.5
    assert session.calls == 1


def test_market_lookup_is_cached():
    session = FakeSession(event())
    data = MarketData(session)
    assert data.market("BTC", 1000) == data.market("BTC", 1000)
    assert session.calls == 1


def test_old_rounds_are_dropped_from_the_cache():
    session = FakeSession(event())
    data = MarketData(session)
    data.market("BTC", 1000)
    data.market("BTC", 1000 + 300 * 50)
    assert list(data._markets) == [("BTC", 1000 + 300 * 50)]
