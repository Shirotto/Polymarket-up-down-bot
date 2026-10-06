import dataclasses
from types import SimpleNamespace as NS

import pytest

from updown_bot.config import load_config
from updown_bot.execution import Broker, best_quote, parse_fill
from updown_bot.strategy import Quote


def test_parse_fill_reads_decimal_amounts():
    fill = parse_fill({"makingAmount": "4.5", "takingAmount": "5.0"}, stake=4.5, ref_price=0.92)
    assert (fill.usdc, fill.shares) == (4.5, 5.0)
    assert fill.price == pytest.approx(0.90)


def test_parse_fill_reads_base_units():
    fill = parse_fill({"makingAmount": "4500000", "takingAmount": "5000000"}, 4.5, 0.92)
    assert (fill.usdc, fill.shares) == (4.5, 5.0)


@pytest.mark.parametrize("resp", [None, "ok", {}, {"makingAmount": "", "takingAmount": None}])
def test_parse_fill_falls_back_to_the_order_it_sent(resp):
    fill = parse_fill(resp, stake=4.6, ref_price=0.92)
    assert fill.usdc == 4.6
    assert fill.shares == pytest.approx(5.0)
    assert fill.price == pytest.approx(0.92)


def test_parse_fill_estimates_missing_shares_from_the_amount_paid():
    fill = parse_fill({"makingAmount": "2.3"}, stake=4.6, ref_price=0.92)
    assert fill.usdc == 2.3
    assert fill.shares == pytest.approx(2.5)


def test_dry_run_broker_never_touches_the_order_api():
    broker = Broker(dataclasses.replace(load_config({}), dry_run=True))

    class Explodes:
        def __getattr__(self, name):
            raise AssertionError(f"dry run called {name}")

    broker._client = Explodes()
    fill = broker.buy("btc-updown-5m-1000", "UP", "111", ask=0.80, stake=4.0, deadline=0)
    assert fill.shares == pytest.approx(5.0)
    assert fill.price == 0.80
    assert broker.sell("btc-updown-5m-1000", "111", 5.0) == 5.0


def book(asset_id, bids, asks):
    return NS(
        asset_id=asset_id,
        bids=[NS(price=p, size="10") for p in bids],
        asks=[NS(price=p, size="10") for p in asks],
    )


def test_best_quote_does_not_rely_on_the_order_of_levels():
    # The API lists both sides with the best price last.
    quote = best_quote(book("1", ["0.01", "0.40", "0.55"], ["0.99", "0.70", "0.56"]))
    assert quote == Quote(bid=0.55, ask=0.56)
    assert best_quote(book("1", ["0.55", "0.01"], ["0.56", "0.99"])) == Quote(0.55, 0.56)


def test_best_quote_of_an_empty_side():
    assert best_quote(book("1", [], ["0.56"])) == Quote(None, 0.56)
    assert best_quote(NS(asset_id="1", bids=None, asks=None)) == Quote(None, None)


def test_quotes_are_fetched_in_one_request():
    broker = Broker(load_config({}))
    calls = []

    class Client:
        def get_order_books(self, params):
            calls.append([p.token_id for p in params])
            return [book("111", ["0.55"], ["0.56"])]

    broker._client = Client()
    quotes = broker.quotes(["111", "222", "111"])
    assert calls == [["111", "222"]]
    assert quotes == {"111": Quote(0.55, 0.56), "222": Quote(None, None)}


def test_a_failed_request_gives_empty_quotes():
    broker = Broker(load_config({}))

    class Client:
        def get_order_books(self, params):
            raise RuntimeError("503")

    broker._client = Client()
    assert broker.quotes(["111"]) == {"111": Quote(None, None)}
    assert broker.quotes([]) == {}
