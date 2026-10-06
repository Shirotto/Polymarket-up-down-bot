from updown_bot import wallet


def test_balance_of_calldata_pads_the_address():
    data = wallet.balance_of_calldata("0xAbC0000000000000000000000000000000000001")
    assert data == "0x70a08231" + "0" * 24 + "abc0000000000000000000000000000000000001"
    assert len(data) == 2 + 8 + 64


def test_usdc_balance_converts_from_base_units(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "result": hex(12_345_678)}

    monkeypatch.setattr(wallet.requests, "post", lambda *a, **k: Response())
    assert wallet.usdc_balance("https://rpc.example", "0x" + "aa" * 20) == 12.345678


def test_usdc_balance_is_none_on_an_rpc_error(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}}

    monkeypatch.setattr(wallet.requests, "post", lambda *a, **k: Response())
    assert wallet.usdc_balance("https://rpc.example", "0x" + "aa" * 20) is None
