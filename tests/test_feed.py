import json

from updown_bot.feed import BinanceFeed, parse_tick


def tick(stream="btcusdt@ticker", **data):
    return json.dumps({"stream": stream, "data": data})


def test_parse_tick():
    assert parse_tick(tick(c="64250.10")) == ("BTC", 64250.10)
    assert parse_tick(tick("dogeusdt@ticker", c="0.1234")) == ("DOGE", 0.1234)


def test_parse_tick_ignores_everything_else():
    assert parse_tick("") is None
    assert parse_tick("not json") is None
    assert parse_tick(tick("btcusdt@trade", c="1")) is None
    assert parse_tick(tick("btceur@ticker", c="1")) is None
    assert parse_tick(tick(c="abc")) is None
    assert parse_tick(tick(o="1")) is None
    assert parse_tick(json.dumps({"result": None, "id": 1})) is None


def test_stale_prices_are_not_served(monkeypatch):
    feed = BinanceFeed(["btc"])
    assert feed.price("BTC") is None
    feed._prices["BTC"] = (64_000.0, 1000.0)
    monkeypatch.setattr("updown_bot.feed.time.time", lambda: 1004.0)
    assert feed.price("btc") == 64_000.0
    monkeypatch.setattr("updown_bot.feed.time.time", lambda: 1006.0)
    assert feed.price("btc") is None


def test_stream_url_lists_every_asset():
    url = BinanceFeed(["BTC", "eth"])._url()
    assert url.endswith("?streams=btcusdt@ticker/ethusdt@ticker")
